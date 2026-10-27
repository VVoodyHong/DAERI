"""Approved evidence keeps its original target, scope and completeness across plans."""

import copy

import pytest

from daeri.application.runtime import Runtime
from daeri.infrastructure.resources import ResourceRegistry
from daeri.infrastructure.storage import Store
from test_execution_flow import approve, begin, configured, execution, resource


def replace_plan(runtime, saved, draft, first):
    revised = copy.deepcopy(draft)
    search = runtime.call("daeri_memory_search", {"task_id": saved["task_id"], "query": "Reuse the completed read"})
    revised.update(memory_search_refs=[search["data"]["memory_search_ref"]],
        previous_plan_id=saved["plan_id"], carried_evidence_refs=[first["result_ref"]])
    result = runtime.call("daeri_plan_submit", {"request_id": "replacement", "task_id": saved["task_id"],
        "expected_task_revision": first["task_revision"], "draft": revised})
    assert result["status"] == "ok", result
    saved = result["data"]
    return saved, revised, approve(runtime, saved, "-replacement")


def report(first, state="succeeded"):
    return {'observation': 'The saved resource returned a row',
            'coverage': 'complete' if state == 'succeeded' else 'The resource reported a truncated result'}


@pytest.mark.parametrize("changed", [None, "resource", "scope"])
def test_host_can_report_carried_evidence_with_its_original_target_and_scope(configured, changed):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    first = runtime.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))["data"]
    step = copy.deepcopy(draft["steps"][1 if changed == "resource" else 0])
    step["step_id"] = "read-0"
    if changed == "scope":
        step["scope"] = {"one_of": {"key": ["failed"]}}
    draft["steps"] = [step]
    draft["completion_criteria"][0]["step_ids"] = ["read-0"]
    saved, _, approved = replace_plan(runtime, saved, draft, first)
    result = runtime.call("daeri_result_submit", {"request_id": "report", "task_id": saved["task_id"],
        "expected_task_revision": approved["task_revision"], "plan_id": saved["plan_id"],
        "plan_version": saved["plan_version"], "report": report(first), 'evidence_refs': [first['result_ref']]})
    assert result["status"] == "ok", result
    assert result["data"]["task_state"] == "succeeded"
    proof = runtime.store.read_record(saved['task_id'], result['data']['report_ref'])['payload']
    assert proof['evidence_refs'] == [first['result_ref']]
    assert len(runtime.store.get_task(saved["task_id"])["calls"]) == 1


def test_carried_read_satisfies_approved_dependency_without_repeating_it(configured):
    runtime, profile = configured
    saved, draft = begin(runtime, profile, max_calls=2)
    first = runtime.call("daeri_capability_execute", execution(saved, approve(runtime, saved), draft["steps"][0]))["data"]
    draft["steps"][1]["depends_on"] = ["read-0"]
    saved, draft, approved = replace_plan(runtime, saved, draft, first)
    args = execution(saved, approved, draft["steps"][1], call_id="second-read")
    result = runtime.call("daeri_capability_execute", args)
    assert result["status"] == "ok", result
    assert result["data"]["inline_result"]["data"]["rows"][0]["amount"] == "7.00"
    assert result["data"]["budget_remaining"]["remaining_tool_calls"] == 0
    assert len(runtime.store.get_task(saved["task_id"])["calls"]) == 2


@pytest.mark.parametrize("changed", ["resource", "scope"])
def test_carried_dependency_rejects_a_reused_step_with_a_different_target_or_scope(configured, changed):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    first = runtime.call("daeri_capability_execute", execution(saved, approve(runtime, saved), draft["steps"][0]))["data"]
    if changed == "resource":
        draft["steps"][0] = {**draft["steps"][1], "step_id": "read-0"}
    else:
        draft["steps"][0]["scope"] = {"one_of": {"key": ["failed"]}}
    draft["steps"][1]["depends_on"] = ["read-0"]
    saved, draft, approved = replace_plan(runtime, saved, draft, first)
    args = execution(saved, approved, draft["steps"][1], call_id="second-read")
    result = runtime.call("daeri_capability_execute", args)
    assert result["error"]["code"] == "SCOPE_VIOLATION", result
    assert len(runtime.store.get_task(saved["task_id"])["calls"]) == 1


def test_truncated_lower_stdio_stays_partial_on_execution_replay_and_archived_read(tmp_path):
    store = Store(tmp_path)
    item = resource()
    item["connection"]["env"]["FIXTURE_TRUNCATED"] = "true"
    profile = store.put_profile("truncated", resources=[item])
    ResourceRegistry(store).diagnose(profile, item["instance_id"])
    store.close()
    runtime = Runtime(tmp_path)
    try:
        saved, draft = begin(runtime, profile)
        args = execution(saved, approve(runtime, saved), draft["steps"][0])
        result = runtime.call("daeri_capability_execute", args)
        assert result["status"] == "partial", result
        assert result["error"]["code"] == "RESULT_TRUNCATED"
        assert result["data"]["call_state"] == "completed"
        assert result["data"]["inline_result"]["data"]["rows"][0]["id"] == "one"
        assert result["data"]["warnings"]
        assert runtime.call("daeri_capability_execute", args) == result
        first = result["data"]
        read_args = {"task_id": saved["task_id"], "record_ref": first["result_ref"]}
        read = runtime.call("daeri_result_get", read_args)
        assert read["status"] == "partial", read
        assert read["error"]["code"] == "RESULT_TRUNCATED"
        assert read["data"]["coverage"]["source_complete"] is False
        assert read["data"]["coverage"]["missing_scope"]
        stored = runtime.call("daeri_result_submit", {"request_id": "partial-report", "task_id": saved["task_id"],
            "expected_task_revision": first["task_revision"], "plan_id": saved["plan_id"],
            "plan_version": saved["plan_version"], "report": report(first, "partial"),
            'state': 'partial', 'evidence_refs': [first['result_ref']]})
        assert stored["status"] == "ok", stored
        assert runtime.call("daeri_result_get", read_args)["status"] == "partial"
        assert runtime.call("daeri_result_get", {"task_id": saved["task_id"], "record_ref": stored["data"]["report_ref"]})["status"] == "ok"
    finally:
        runtime.close()
