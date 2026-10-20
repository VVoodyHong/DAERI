"""Approved evidence keeps its original target, scope and completeness across plans."""

import copy

import pytest

from daeri.engine import Engine
from daeri.resources import ResourceRegistry
from daeri.storage import Store
from test_execution_flow import approve, begin, configured, execution, resource


def replace_plan(engine, saved, draft, first):
    revised = copy.deepcopy(draft)
    search = engine.call("daeri_memory_search", {"task_id": saved["task_id"], "query": "Reuse the completed read"})
    revised.update(memory_search_refs=[search["data"]["memory_search_ref"]],
        previous_plan_id=saved["plan_id"], carried_evidence_refs=[first["result_ref"]])
    result = engine.call("daeri_plan_submit", {"request_id": "replacement", "task_id": saved["task_id"],
        "expected_task_revision": first["task_revision"], "draft": revised})
    assert result["status"] == "ok", result
    saved = result["data"]
    return saved, revised, approve(engine, saved, "-replacement")


def report(first, state="succeeded"):
    check = {"criterion_id": "read", "status": "met" if state == "succeeded" else "unknown",
        "method": "Compare the recorded read with the approved criterion", "evidence_refs": [first["result_ref"]]}
    if state != "succeeded":
        check["reason"] = "The resource explicitly reported a truncated result"
    return {"summary": "Report the recorded read", "proposed_task_state": state,
        "findings": [{"kind": "observation", "text": "The saved resource returned a row", "evidence_refs": [first["result_ref"]]}],
        "tables": [], "checks": [check], "limitations": [] if state == "succeeded" else [check["reason"]],
        "uncertain_effects": [], "artifacts": [], "next_actions": []}


@pytest.mark.parametrize("changed", [None, "resource", "scope"])
def test_completion_credits_only_a_carried_read_of_the_same_target_and_scope(configured, changed):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    first = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))["data"]
    step = copy.deepcopy(draft["steps"][1 if changed == "resource" else 0])
    step["step_id"] = "read-0"
    if changed == "scope":
        step["scope"] = {"one_of": {"key": ["failed"]}}
    draft["steps"] = [step]
    draft["completion_criteria"][0]["step_ids"] = ["read-0"]
    saved, _, approved = replace_plan(engine, saved, draft, first)
    result = engine.call("daeri_result_submit", {"request_id": "report", "task_id": saved["task_id"],
        "expected_task_revision": approved["task_revision"], "plan_id": saved["plan_id"],
        "plan_version": saved["plan_version"], "report": report(first)})
    if changed:
        assert result["status"] == "error", result
        assert result["error"]["code"] == "RESULT_VALIDATION_FAILED"
        assert engine.store.get_task(saved["task_id"])["state"] == "running"
    else:
        assert result["status"] == "ok", result
        assert result["data"]["task_state"] == "succeeded"
    assert len(engine.store.get_task(saved["task_id"])["calls"]) == 1


@pytest.mark.parametrize("bound", [False, True])
def test_carried_read_satisfies_approved_dependency_without_repeating_it(configured, bound):
    engine, profile = configured
    saved, draft = begin(engine, profile, max_calls=2)
    first = engine.call("daeri_capability_execute", execution(saved, approve(engine, saved), draft["steps"][0]))["data"]
    draft["steps"][1]["depends_on"] = ["read-0"]
    if bound:
        draft["steps"][1].update(input_bindings=[{"binding_id": "amount", "source_step_id": "read-0",
            "source_pointer": "/data/rows/0/amount", "target_pointer": None}],
            conditions=[{"binding_id": "amount", "operator": "eq", "value": "10.00"}])
    saved, draft, approved = replace_plan(engine, saved, draft, first)
    args = execution(saved, approved, draft["steps"][1], call_id="second-read")
    if bound:
        args["bindings"] = [{"binding_id": "amount", "source_call_id": "call-1"}]
    result = engine.call("daeri_capability_execute", args)
    assert result["status"] == "ok", result
    assert result["data"]["inline_result"]["data"]["rows"][0]["amount"] == "7.00"
    assert result["data"]["budget_remaining"]["remaining_tool_calls"] == 0
    assert len(engine.store.get_task(saved["task_id"])["calls"]) == 2


@pytest.mark.parametrize("changed", ["resource", "scope"])
def test_carried_binding_rejects_a_reused_step_with_a_different_target_or_scope(configured, changed):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    first = engine.call("daeri_capability_execute", execution(saved, approve(engine, saved), draft["steps"][0]))["data"]
    if changed == "resource":
        draft["steps"][0] = {**draft["steps"][1], "step_id": "read-0"}
    else:
        draft["steps"][0]["scope"] = {"one_of": {"key": ["failed"]}}
    draft["steps"][1].update(depends_on=["read-0"], input_bindings=[{"binding_id": "amount", "source_step_id": "read-0",
        "source_pointer": "/data/rows/0/amount", "target_pointer": None}])
    saved, draft, approved = replace_plan(engine, saved, draft, first)
    args = execution(saved, approved, draft["steps"][1], call_id="second-read")
    args["bindings"] = [{"binding_id": "amount", "source_call_id": "call-1"}]
    result = engine.call("daeri_capability_execute", args)
    assert result["error"]["code"] == "BINDING_INVALID", result
    assert len(engine.store.get_task(saved["task_id"])["calls"]) == 1


def test_carried_read_can_prove_an_approved_branch_not_applicable(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    first = engine.call("daeri_capability_execute", execution(saved, approve(engine, saved), draft["steps"][0]))["data"]
    draft["steps"][1].update(depends_on=["read-0"], input_bindings=[{"binding_id": "amount", "source_step_id": "read-0",
        "source_pointer": "/data/rows/0/amount", "target_pointer": None}],
        conditions=[{"binding_id": "amount", "operator": "eq", "value": "0"}])
    draft["completion_criteria"][0]["step_ids"] = ["read-1"]
    saved, draft, approved = replace_plan(engine, saved, draft, first)
    args = execution(saved, approved, draft["steps"][1], call_id="unneeded-read")
    args["bindings"] = [{"binding_id": "amount", "source_call_id": "call-1"}]
    assert engine.call("daeri_capability_execute", args)["error"]["code"] == "CONDITION_NOT_MET"
    candidate = report(first)
    candidate["checks"][0].update(status="not_applicable", reason="The carried amount is 10.00, so the approved equality to 0 is false")
    result = engine.call("daeri_result_submit", {"request_id": "excluded-report", "task_id": saved["task_id"],
        "expected_task_revision": approved["task_revision"], "plan_id": saved["plan_id"],
        "plan_version": saved["plan_version"], "report": candidate})
    assert result["status"] == "ok", result
    assert result["data"]["task_state"] == "succeeded"
    assert result["data"]["validation"]["completion"] == "met"
    assert len(engine.store.get_task(saved["task_id"])["calls"]) == 1


def test_truncated_lower_stdio_stays_partial_on_execution_replay_and_archived_read(tmp_path):
    store = Store(tmp_path)
    item = resource()
    item["connection"]["env"]["FIXTURE_TRUNCATED"] = "true"
    profile = store.put_profile("truncated", resources=[item])
    ResourceRegistry(store).diagnose(profile, item["instance_id"])
    store.close()
    engine = Engine(tmp_path)
    try:
        saved, draft = begin(engine, profile)
        args = execution(saved, approve(engine, saved), draft["steps"][0])
        result = engine.call("daeri_capability_execute", args)
        assert result["status"] == "partial", result
        assert result["error"]["code"] == "RESULT_TRUNCATED"
        assert result["data"]["call_state"] == "completed"
        assert result["data"]["inline_result"]["data"]["rows"][0]["id"] == "one"
        assert result["data"]["warnings"]
        assert engine.call("daeri_capability_execute", args) == result
        first = result["data"]
        read_args = {"task_id": saved["task_id"], "record_ref": first["result_ref"]}
        read = engine.call("daeri_result_get", read_args)
        assert read["status"] == "partial", read
        assert read["error"]["code"] == "RESULT_TRUNCATED"
        assert read["data"]["coverage"]["source_complete"] is False
        assert read["data"]["coverage"]["missing_scope"]
        stored = engine.call("daeri_result_submit", {"request_id": "partial-report", "task_id": saved["task_id"],
            "expected_task_revision": first["task_revision"], "plan_id": saved["plan_id"],
            "plan_version": saved["plan_version"], "report": report(first, "partial")})
        assert stored["status"] == "ok", stored
        assert engine.call("daeri_result_get", read_args)["status"] == "partial"
        assert engine.call("daeri_result_get", {"task_id": saved["task_id"], "record_ref": stored["data"]["report_ref"]})["status"] == "ok"
    finally:
        engine.close()
