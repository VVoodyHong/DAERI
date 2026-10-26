"""Ordinary work uses targets and user decisions while runtime owns bookkeeping."""

import copy
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_execution_flow import configured, begin, approve, comparison_report
from test_memory_flow import remembered


def simple_plan(profile):
    return {
        "purpose": "Compare current data",
        "steps": [
            {"step_id": "read-" + str(index), "description": "Read approved key",
             "instance_id": item["instance_id"], "capability_id": "get",
             "scope": {"equals": {"key": "orders"}}}
            for index, item in enumerate(profile["resources"])
        ],
        "output": {"format": "text"},
        "completion_criteria": [{"criterion_id": "read", "description": "Read both sources",
                                 "step_ids": ["read-0", "read-1"]}],
    }


def create_simple_task(runtime, profile):
    created = runtime.call("daeri_task_create", {
        "request_text": "Compare configured key data",
        "context": {"profile_id": profile["profile_id"]},
    })
    assert created["status"] == "ok", created
    return created["data"]["task_id"]


def test_workflow_needs_only_plan_user_decision_and_read_parameters(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    saved = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": simple_plan(profile)})
    assert saved["status"] == "ok", saved
    plan_id = saved["data"]["plan_id"]
    assert runtime.store.get_task(task_id)["approvals"][-1]["state"] == "awaiting_response"
    blocked = runtime.call("daeri_capability_execute", {
        "task_id": task_id, "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert blocked["status"] == "error"
    assert runtime.store.get_task(task_id)["calls"] == []
    approved = runtime.call("daeri_approval_request", {
        "task_id": task_id, "plan_id": plan_id,
        "decision": "approve", "user_response": "진행해",
    })
    assert approved["status"] == "ok", approved
    results = []
    for step_id in ("read-0", "read-1"):
        result = runtime.call("daeri_capability_execute", {
            "task_id": task_id, "step_id": step_id, "parameters": {"key": "orders"},
        })
        assert result["status"] == "ok", result
        results.append(result["data"])
    submitted = runtime.call("daeri_result_submit", {
        "task_id": task_id, "report": comparison_report(*results),
    })
    assert submitted["status"] == "ok", submitted
    assert submitted["data"]["task_state"] == "succeeded"
    assert submitted["data"]["memory"]["state"] == "skipped"


def test_corrected_plan_can_retry_without_request_id_bookkeeping(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    draft = simple_plan(profile)
    draft["steps"][0]["depends_on"] = ["missing-step"]
    rejected = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})
    assert rejected["status"] == "error", rejected
    draft["steps"][0]["depends_on"] = []
    corrected = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})
    assert corrected["status"] == "ok", corrected


def test_repeated_plan_and_report_return_existing_records(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    args = {"task_id": task_id, "draft": simple_plan(profile)}
    first = runtime.call("daeri_plan_submit", args)
    repeated = runtime.call("daeri_plan_submit", args)
    assert repeated["data"]["plan_id"] == first["data"]["plan_id"]
    assert len(runtime.store.get_task(task_id)["plans"]) == 1
    approved = {"task_id": task_id, "plan_id": first["data"]["plan_id"],
                "decision": "approve", "user_response": "진행해"}
    assert runtime.call("daeri_approval_request", approved)["status"] == "ok"
    assert runtime.call("daeri_approval_request", approved)["status"] == "ok"
    results = []
    for step_id in ("read-0", "read-1"):
        result = runtime.call("daeri_capability_execute", {
            "task_id": task_id, "step_id": step_id, "parameters": {"key": "orders"},
        })
        assert result["status"] == "ok", result
        results.append(result["data"])
    report_args = {"task_id": task_id, "report": comparison_report(*results)}
    submitted = runtime.call("daeri_result_submit", report_args)
    replayed = runtime.call("daeri_result_submit", report_args)
    assert submitted["status"] == replayed["status"] == "ok", (submitted, replayed)
    assert replayed["data"] == submitted["data"]
    records = runtime.store.list_records(task_id)
    assert len([item for item in records if item["record_kind"] == "report"]) == 1


def test_same_plan_replays_after_runtime_adds_search_limitations(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    searched = runtime.call("daeri_memory_search", {"task_id": task_id, "query": "compare key data"})["data"]
    assert searched["limitations"]
    draft = simple_plan(profile)
    draft["memory_search_refs"] = [searched["memory_search_ref"]]
    args = {"task_id": task_id, "draft": draft}
    first = runtime.call("daeri_plan_submit", args)
    repeated = runtime.call("daeri_plan_submit", args)
    assert repeated["status"] == "ok", repeated
    assert repeated["data"]["plan_id"] == first["data"]["plan_id"]
    assert len(runtime.store.get_task(task_id)["plans"]) == 1


def test_same_plan_retry_reports_invalidated_selected_memory_as_a_limitation(remembered):
    runtime, profile, _, _, _, _, _, reported = remembered
    memory_ref = reported["memory"]["items"][0]["memory_ref"]
    task_id = create_simple_task(runtime, profile)
    searched = runtime.call("daeri_memory_search", {"task_id": task_id, "query": "Cache 비교"})["data"]
    assert searched["limitations"]
    draft = simple_plan(profile)
    draft.update(memory_search_refs=[searched["memory_search_ref"]], memory_refs=[memory_ref])
    args = {"task_id": task_id, "draft": draft}
    first = runtime.call("daeri_plan_submit", args)
    assert first["status"] == "ok", first
    _, canonical = runtime.memory._row(profile["profile_id"], memory_ref)
    runtime.memory.invalidate(profile["profile_id"], memory_ref, reason="실제 확인으로 정정함",
                              source_records=canonical["_sources"])
    repeated = runtime.call("daeri_plan_submit", args)
    assert repeated["status"] == "ok", repeated
    assert repeated["data"]["plan_id"] == first["data"]["plan_id"]
    assert repeated["data"]["recall_limitations"] == [{"memory_ref": memory_ref, "error_code": "MEMORY_CONFLICT"}]


def test_clarification_retires_prior_approval_and_subagent_before_resuming(configured):
    from test_subagents import create_subagent

    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    draft = simple_plan(profile)
    saved = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})["data"]
    decision = {"task_id": task_id, "plan_id": saved["plan_id"],
                "decision": "approve", "user_response": "진행해"}
    assert runtime.call("daeri_approval_request", decision)["status"] == "ok"
    create_subagent(runtime, saved, ["read-0"])
    question = runtime.call("daeri_task_update", {"task_id": task_id,
        "patch": {"question": "조사 범위를 어떻게 바꿀까요?"}})
    assert question["status"] == "ok", question
    changed = runtime.call("daeri_task_update", {"task_id": task_id,
        "patch": {"clarification": "조회 기간을 바꾸고 기존 계획은 진행하지 마."}})
    assert changed["data"]["state"] == "planning"
    current = runtime.store.get_task(task_id)
    assert current["delegations"][0]["state"] == "revoked"
    resumed = runtime.call("daeri_task_resume", {"task_id": task_id})
    assert resumed["data"]["state"] == "awaiting_approval", resumed
    assert resumed["data"]["resumed"] is False
    presented = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})
    assert presented["status"] == "ok", presented
    assert runtime.call("daeri_approval_request", decision)["status"] == "ok"


def test_host_can_preserve_a_preplan_failure_without_synthetic_local_check(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    report = {"summary": "연결된 자료로 요청한 조사를 구성할 수 없습니다.", "proposed_task_state": "failed",
        "findings": [], "checks": [], "limitations": ["요청한 연결이 현재 제공되지 않습니다."],
        "uncertain_effects": [], "artifacts": [], "next_actions": ["연결 설정을 확인하세요."]}
    saved = runtime.call("daeri_result_submit", {"task_id": task_id, "report": report})
    assert saved["status"] == "ok", saved
    assert saved["data"]["task_state"] == "failed"
    loaded = runtime.call("daeri_result_get", {"task_id": task_id, "record_ref": saved["data"]["report_ref"]})
    assert loaded["data"]["payload"]["summary"] == report["summary"]


def test_approval_stays_bound_to_the_plan_the_user_saw(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    draft = simple_plan(profile)
    first = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})
    draft["purpose"] = "Compare a changed business objective"
    second = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})
    assert second["status"] == "ok", second
    stale = runtime.call("daeri_approval_request", {
        "task_id": task_id, "plan_id": first["data"]["plan_id"],
        "decision": "approve", "user_response": "진행해",
    })
    assert stale["error"]["code"] == "APPROVAL_UNAVAILABLE", stale
    assert runtime.store.get_task(task_id)["state"] == "awaiting_approval"


def test_unrelated_profile_change_keeps_approved_reads_usable(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    runtime.store.put_profile(profile["name"], resources=profile["resources"], core_context=["Updated service description"])
    result = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert result["status"] == "ok", result


def test_used_target_change_still_requires_new_plan(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    changed = copy.deepcopy(profile["resources"])
    changed[0]["target"]["host"] = "different.invalid"
    runtime.store.put_profile(profile["name"], resources=changed)
    result = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert result["status"] == "error", result
    assert runtime.store.get_task(saved["task_id"])["calls"] == []


def test_restart_recovers_interrupted_read_and_allows_new_observation(configured, monkeypatch):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    task = runtime.store.get_task(saved["task_id"])
    task["owner_id"] = "999999999:dead"
    task["calls"].append({
        "call_id": "interrupted", "plan_id": saved["plan_id"], "plan_version": saved["plan_version"],
        "step_id": "read-0", "instance_id": "cache-a", "capability_id": "get", "contract_ref": "old",
        "call_state": "pending", "dispatched": True, "external_effect": "none", "result_ref": None,
        "reserved_runtime_ms": 60000, "runtime_ms": 0, "error_code": None,
    })
    task["reserved"]["runtime_ms"] = 60000
    with runtime.store.transaction():
        runtime._save(task)
    monkeypatch.setattr("daeri.application.runtime.process_alive", lambda unused_pid: False)
    resumed = runtime.call("daeri_task_resume", {"task_id": saved["task_id"]})
    assert resumed["status"] == "ok", resumed
    assert resumed["data"]["unresolved_call_ids"] == []
    recovered = runtime.store.get_task(saved["task_id"])
    assert recovered["calls"][0]["call_state"] == "failed"
    assert recovered["reserved"]["runtime_ms"] == 0
    observed = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert observed["status"] == "ok", observed


def test_new_observations_do_not_need_call_ids_and_legacy_replays_ignore_revision(configured):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    args = {"task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"}}
    first = runtime.call("daeri_capability_execute", args)
    second = runtime.call("daeri_capability_execute", args)
    assert first["status"] == second["status"] == "ok", (first, second)
    assert first["data"]["call_id"] != second["data"]["call_id"]
    from test_execution_flow import execution
    explicit = execution(saved, {**approved, "task_revision": second["data"]["task_revision"]},
                         draft["steps"][0], call_id="explicit")
    original = runtime.call("daeri_capability_execute", explicit)
    assert original["status"] == "ok", original
    explicit["expected_task_revision"] = original["data"]["task_revision"]
    replayed = runtime.call("daeri_capability_execute", explicit)
    assert replayed["status"] == "ok", replayed
    assert len(runtime.store.get_task(saved["task_id"])["calls"]) == 3


def test_expired_recall_log_does_not_revoke_approved_resource_read(configured):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approve(runtime, saved)
    with runtime.store.transaction():
        runtime.store._db.execute("UPDATE records SET blocked=1 WHERE record_ref=?",
                                  (draft["memory_search_refs"][0],))
    result = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert result["status"] == "ok", result


def test_paused_task_can_resume_and_accept_a_new_user_decision(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    saved = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": simple_plan(profile)})["data"]
    decision = {"task_id": task_id, "plan_id": saved["plan_id"],
                "decision": "approve", "user_response": "진행해"}
    assert runtime.call("daeri_approval_request", decision)["status"] == "ok"
    paused = runtime.call("daeri_task_update", {"task_id": saved["task_id"], "action": "pause"})
    assert paused["status"] == "ok", paused
    resumed = runtime.call("daeri_task_resume", {"task_id": saved["task_id"]})
    assert resumed["status"] == "pending", resumed
    approved = runtime.call("daeri_approval_request", decision)
    assert approved["status"] == "ok", approved
    assert approved["data"]["state"] == "approved", approved
    observed = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert observed["status"] == "ok", observed


def test_same_live_observation_replays_while_different_reads_run_in_parallel(configured):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    draft = simple_plan(profile)
    draft["steps"][0]["scope"] = {"one_of": {"key": ["orders", "slow"]}}
    saved = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})["data"]
    assert runtime.call("daeri_approval_request", {
        "task_id": task_id, "plan_id": saved["plan_id"],
        "decision": "approve", "user_response": "진행해",
    })["status"] == "ok"
    slow = {"task_id": task_id, "step_id": "read-0", "parameters": {"key": "slow"}}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.call, "daeri_capability_execute", slow)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            calls = runtime.store.get_task(task_id)["calls"]
            if calls and calls[0]["dispatched"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("The real read did not start")
        same = runtime.call("daeri_capability_execute", slow)
        assert same["status"] == "pending", same
        assert same["data"]["call_id"] == calls[0]["call_id"]
        other = runtime.call("daeri_capability_execute", {
            "task_id": task_id, "step_id": "read-0", "parameters": {"key": "orders"},
        })
        assert other["status"] == "ok", other
        completed = future.result(timeout=10)
        assert completed["status"] == "ok", completed
    task = runtime.store.get_task(task_id)
    assert len(task["calls"]) == task["usage"]["tool_calls"] == 2
    assert task["reserved"]["runtime_ms"] == 0


def test_exact_legacy_call_receipt_remains_replayable_after_upgrade(configured):
    from daeri.core.security import redact
    from test_execution_flow import execution
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    args = execution(saved, approved, draft["steps"][0])
    original = runtime.call("daeri_capability_execute", args)
    assert original["status"] == "ok", original
    legacy = hashlib.sha256(json.dumps(redact(args), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with runtime.store.transaction():
        runtime.store._db.execute("UPDATE requests SET fingerprint=? WHERE scope=? AND request_key=?",
            (legacy, "call:" + saved["task_id"], args["call_id"]))
    replayed = runtime.call("daeri_capability_execute", args)
    assert replayed["status"] == "ok", replayed
    assert replayed["data"]["result_ref"] == original["data"]["result_ref"]
    assert len(runtime.store.get_task(saved["task_id"])["calls"]) == 1
    args["parameters"] = {"key": "failed"}
    changed = runtime.call("daeri_capability_execute", args)
    assert changed["error"]["code"] == "CALL_ID_CONFLICT", changed


def test_exact_legacy_task_request_does_not_create_a_duplicate_after_upgrade(configured):
    from daeri.core.security import redact
    runtime, profile = configured
    args = {"request_id": "old-request", "request_text": "Compare data",
            "context": {"profile_id": profile["profile_id"]}}
    created = runtime.call("daeri_task_create", args)
    assert created["status"] == "ok", created
    legacy = hashlib.sha256(json.dumps(redact(args), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with runtime.store.transaction():
        runtime.store._db.execute("UPDATE requests SET fingerprint=? WHERE scope=? AND request_key=?",
            (legacy, "daeri_task_create:local", args["request_id"]))
    replayed = runtime.call("daeri_task_create", args)
    assert replayed["status"] == "ok", replayed
    assert replayed["data"]["task_id"] == created["data"]["task_id"]


def test_historical_value_binding_requires_replanning(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    task = runtime.store.get_task(saved["task_id"])
    header = task["plans"][-1]
    body = runtime.store.read_record(task["task_id"], header["body_ref"])["payload"]
    body["draft"]["steps"][0]["input_bindings"] = [{
        "binding_id": "old-binding", "source_step_id": "read-1",
        "source_pointer": "/data/rows/0/id", "target_pointer": "/key",
    }]
    with runtime.store.transaction():
        header["body_ref"] = runtime.store.write_record(task["task_id"], "plan", body)
        runtime._save(task)
    result = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert result["error"]["code"] == "UNSUPPORTED_CONSTRAINT", result
    assert runtime.store.get_task(saved["task_id"])["calls"] == []


def test_current_retention_policy_applies_when_read_finishes(configured, monkeypatch):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    execute = runtime.registry.execute

    def change_policy_before_result_is_recorded(invocation, **kwargs):
        result = execute(invocation, **kwargs)
        resources = copy.deepcopy(profile["resources"])
        resources[0]["policy"]["retained_fields"] = []
        runtime.store.put_profile(profile["name"], resources=resources)
        return result

    monkeypatch.setattr(runtime.registry, "execute", change_policy_before_result_is_recorded)
    result = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert result["status"] == "ok", result
    ref = result["data"]["result_ref"]
    body = runtime.store.read_record(saved["task_id"], ref)["payload"]
    assert body["retained_paths"] == []
    with runtime.store.transaction():
        task = runtime.store.get_task(saved["task_id"])
        runtime._finish(task, "partial")
        runtime._save(task)
    assert runtime.store.read_record(saved["task_id"], ref)["payload"]["result"] == {}


def test_completed_read_during_recall_does_not_invalidate_search(configured, monkeypatch):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    search = runtime.memory.search

    def search_while_read_finishes(*args, **kwargs):
        found = search(*args, **kwargs)
        read = runtime.call("daeri_capability_execute", {
            "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"},
        })
        assert read["status"] == "ok", read
        return found

    monkeypatch.setattr(runtime.memory, "search", search_while_read_finishes)
    recalled = runtime.call("daeri_memory_search", {"task_id": saved["task_id"], "query": "key comparison"})
    assert recalled["status"] in {"ok", "partial"}, recalled
    assert recalled["data"]["task_revision"] == runtime.store.get_task(saved["task_id"])["task_revision"]


@pytest.mark.parametrize("decision", ["decline", "cancel"])
def test_same_plan_can_be_presented_again_after_user_declines_or_cancels(configured, decision):
    runtime, profile = configured
    task_id = create_simple_task(runtime, profile)
    args = {"task_id": task_id, "draft": simple_plan(profile)}
    saved = runtime.call("daeri_plan_submit", args)["data"]
    declined = runtime.call("daeri_approval_request", {
        "task_id": task_id, "plan_id": saved["plan_id"], "decision": decision, "user_response": "지금은 중단해",
    })
    assert declined["status"] == "ok", declined
    presented = runtime.call("daeri_plan_submit", args)
    assert presented["status"] == "ok", presented
    assert presented["data"]["approval_request"]["state"] == "awaiting_response", presented
    approved = runtime.call("daeri_approval_request", {
        "task_id": task_id, "plan_id": presented["data"]["plan_id"],
        "decision": "approve", "user_response": "이제 진행해",
    })
    assert approved["status"] == "ok", approved
    read = runtime.call("daeri_capability_execute", {
        "task_id": task_id, "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert read["status"] == "ok", read


def test_invalidated_selected_memory_is_advisory_for_fresh_reads(remembered):
    runtime, profile, _, _, _, _, _, reported = remembered
    memory_ref = reported["memory"]["items"][0]["memory_ref"]
    task_id = create_simple_task(runtime, profile)
    found = runtime.call("daeri_memory_search", {"task_id": task_id, "query": "Cache 비교"})["data"]
    draft = simple_plan(profile)
    draft.update(memory_search_refs=[found["memory_search_ref"]], memory_refs=[memory_ref])
    saved = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": draft})
    assert saved["status"] == "ok", saved
    _, canonical = runtime.memory._row(profile["profile_id"], memory_ref)
    runtime.memory.invalidate(profile["profile_id"], memory_ref, reason="새 확인 결과로 정정함",
                              source_records=canonical["_sources"])
    approved = runtime.call("daeri_approval_request", {
        "task_id": task_id, "plan_id": saved["data"]["plan_id"],
        "decision": "approve", "user_response": "현재 리소스를 직접 조회해서 확인해",
    })
    assert approved["status"] == "ok", approved
    assert approved["data"]["recall_limitations"] == [{"memory_ref": memory_ref, "error_code": "MEMORY_CONFLICT"}]
    read = runtime.call("daeri_capability_execute", {
        "task_id": task_id, "step_id": "read-0", "parameters": {"key": "orders"},
    })
    assert read["status"] == "ok", read


def test_recalled_memory_can_be_used_during_work_without_initial_preselection(remembered):
    runtime, profile, _, _, _, _, _, reported = remembered
    task_id = create_simple_task(runtime, profile)
    planned = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": simple_plan(profile)})
    assert planned["status"] == "ok", planned
    approved = runtime.call("daeri_approval_request", {
        "task_id": task_id, "plan_id": planned["data"]["plan_id"],
        "decision": "approve", "user_response": "진행해",
    })
    assert approved["status"] == "ok", approved
    memory_ref = reported["memory"]["items"][0]["memory_ref"]
    recalled = runtime.call("daeri_memory_search", {"task_id": task_id, "query": "Cache 비교"})
    assert memory_ref in {item["memory_ref"] for item in recalled["data"]["items"]}
    task = runtime.store.get_task(task_id)
    evidence = runtime._load_evidence(task, memory_ref)
    assert evidence["record_kind"] == "memory"
    assert evidence["payload"]["memory_ref"] == memory_ref


@pytest.mark.parametrize('decision', ['decline', 'cancel', 'request_changes'])
def test_nonapproval_reply_can_be_replayed_without_internal_request_keys(tmp_path, decision):
    from contextlib import closing
    from daeri.application.runtime import Runtime

    with closing(Runtime(tmp_path)) as runtime:
        profile = runtime.store.put_profile('reply-replay', embedding={'provider': 'keyword_only'})
        task_id = create_simple_task(runtime, profile)
        plan = runtime.call('daeri_plan_submit', {'task_id': task_id, 'draft': {
            'purpose': '사용자 조건을 정리한다', 'steps': [], 'output': {'format': 'text'},
            'completion_criteria': [{'criterion_id': 'done', 'description': '조건 확인', 'step_ids': []}]}})
        args = {'task_id': task_id, 'plan_id': plan['data']['plan_id'],
                'decision': decision, 'user_response': '이 계획은 진행하지 마'}
        first = runtime.call('daeri_approval_request', args)
        assert first['status'] == 'ok', first
        repeated = runtime.call('daeri_approval_request', args)
        assert repeated == first
        assert runtime.store._db.execute("SELECT count(*) FROM records WHERE task_id=? AND kind='approval'",
                                          (task_id,)).fetchone()[0] == 1
