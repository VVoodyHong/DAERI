import copy
import hashlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from daeri.engine import Engine
from daeri.resources import ResourceRegistry
from daeri.storage import Store


ROOT = {"segments": []}
MEMORY_NONE = {"categories": [], "source_fields": [], "context_sources": [], "retention": "none", "allow_model_recall": False}


def resource(instance="cache-a", amount="10.00"):
    return {"instance_id": instance, "type": "Cache", "adapter": "redis_read", "target": {"host": "example.invalid", "port": 6379, "db": 0},
        "identity_ref": "test:reader", "connection": {"transport": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).parent / "fixtures" / "data_mcp.py")],
            "env": {"REDIS_HOST": "example.invalid", "REDIS_PORT": "6379", "REDIS_DB": "0", "FIXTURE_AMOUNT": amount}},
        "capabilities": [{"capability_id": "get", "tool_name": "get", "constraints": {"allowed_values": {"key": ["orders", "slow", "failed"]}}}],
        "policy": {"allowed_model_fields": [ROOT], "export_fields": [ROOT], "retained_fields": [ROOT], "masking": [], "memory": MEMORY_NONE}}


@pytest.fixture
def configured(tmp_path):
    store = Store(tmp_path)
    profile = store.put_profile("service", resources=[resource(), resource("cache-b", "7.00")])
    registry = ResourceRegistry(store)
    for item in profile["resources"]:
        registry.diagnose(profile, item["instance_id"])
    store.close()
    engine = Engine(tmp_path)
    yield engine, profile
    engine.close()


def begin(engine, profile, *, max_calls=3, max_runtime=30000, configure=None):
    task = engine.call("daeri_task_create", {"request_id": "create", "request_text": "Compare configured key data", "context": {"profile_id": profile["profile_id"]}})["data"]
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "compare key data"})
    steps, rules = [], []
    for index, item in enumerate(profile["resources"]):
        descriptor = engine.call("daeri_capability_get", {"task_id": task["task_id"], "instance_id": item["instance_id"], "capability_id": "get"})["data"]["descriptor"]
        steps.append({"step_id": "read-"+str(index), "description": "Read approved key", "purpose": "collect", "depends_on": [],
            "instance_id": item["instance_id"], "capability_id": "get", "contract_ref": descriptor["contract_ref"], "scope": {"one_of": {"key": ["orders", "slow", "failed"]}}, "input_bindings": []})
        rules.append({"instance_id": item["instance_id"], **item["policy"]})
    draft = {"purpose": "Compare current data", "steps": steps, "budget": {"max_tool_calls": max_calls, "max_external_requests": None, "max_runtime_ms": max_runtime, "resource_limits": []},
        "output": {"format": "text", "destination": "current conversation", "resource_rules": rules, "table_rules": [], "artifact_rules": []},
        "memory_search_refs": [search["data"]["memory_search_ref"]], "memory_refs": [], "memory_limitations": [],
        "completion_criteria": [{"criterion_id": "read", "description": "Read both sources", "required": True, "step_ids": [step["step_id"] for step in steps]}]}
    if configure:
        configure(draft)
    saved = engine.call("daeri_plan_submit", {"request_id": "plan", "task_id": task["task_id"], "expected_task_revision": task["task_revision"], "draft": draft})
    assert saved["status"] == "ok", saved
    return saved["data"], draft


def approve(engine, saved, suffix=""):
    common = {"task_id": saved["task_id"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"]}
    waiting = engine.call("daeri_approval_request", {**common, "request_id": "ask" + suffix, "expected_task_revision": saved["task_revision"], "stage": "request"})["data"]
    result = engine.call("daeri_approval_request", {**common, "request_id": "approve" + suffix, "expected_task_revision": waiting["task_revision"], "stage": "respond", "approval_request_id": waiting["approval_request_id"], "decision": "approve", "user_response": "Approve this test plan"})
    assert result["status"] == "ok", result
    return result["data"]


def execution(saved, approved, step, *, call_id="call-1", key="orders"):
    return {"task_id": saved["task_id"], "expected_task_revision": approved["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"],
        "step_id": step["step_id"], "call_id": call_id, "approval_id": approved["approval_id"], "instance_id": step["instance_id"], "capability_id": step["capability_id"],
        "contract_ref": step["contract_ref"], "parameters": {"key": key}}


def test_actual_lower_mcp_executes_only_after_approval_and_replays_without_spending(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    args = execution(saved, {"task_revision": saved["task_revision"], "approval_id": "invented"}, draft["steps"][0])
    assert engine.call("daeri_capability_execute", args)["status"] == "error"
    approved = approve(engine, saved)
    args = execution(saved, approved, draft["steps"][0])
    result = engine.call("daeri_capability_execute", args)
    assert result["status"] == "ok", result
    assert result["data"]["inline_result"] == {"data": {"rows": [{"id": "one", "amount": "10.00"}]}}
    assert result["data"]["budget_remaining"]["remaining_tool_calls"] == 2
    assert engine.call("daeri_capability_execute", args)["data"]["task_revision"] == result["data"]["task_revision"]
    args["parameters"]["key"] = "different"
    assert engine.call("daeri_capability_execute", args)["error"]["code"] == "CALL_ID_CONFLICT"


def test_connection_only_cache_recipe_still_requires_task_approval_and_literal_key_scope(tmp_path):
    from daeri.setup import build_setup_resource
    store = Store(tmp_path)
    try:
        item = build_setup_resource(store, 'cache-redis', 'service', 'cache-a',
                                    {'host': 'example.invalid', 'port': 6379, 'db': 0, 'tls': True})
        assert item['capabilities'][0]['constraints'] == {}
        fixture = resource()
        item['connection'].update(command=fixture['connection']['command'],
                                  args=fixture['connection']['args'], auth_env={})
        item['capabilities'][0]['capability_id'] = 'get'
        # Explicit test administrator disclosure; connection setup retains its default policy.
        item['policy'] = fixture['policy']
        profile = store.put_profile('service', resources=[item])
        ResourceRegistry(store).diagnose(profile, 'cache-a')
    finally:
        store.close()
    engine = Engine(tmp_path)
    try:
        def one_key(draft):
            draft['steps'][0]['scope'] = {'equals': {'key': 'orders'}}
        saved, draft = begin(engine, profile, configure=one_key)
        args = execution(saved, {'task_revision': saved['task_revision'], 'approval_id': 'invented'}, draft['steps'][0])
        assert engine.call('daeri_capability_execute', args)['status'] == 'error'
        assert engine.store.get_task(saved['task_id'])['calls'] == []
        approved = approve(engine, saved)
        args = execution(saved, approved, draft['steps'][0], key='another-key')
        assert engine.call('daeri_capability_execute', args)['error']['code'] == 'SCOPE_VIOLATION'
        assert engine.store.get_task(saved['task_id'])['calls'] == []
        args['parameters']['key'] = 'orders'
        result = engine.call('daeri_capability_execute', args)
        assert result['status'] == 'ok', result
        assert result['data']['inline_result']['data']['rows'] == [{'id': 'one', 'amount': '10.00'}]
    finally:
        engine.close()


def test_scope_budget_and_old_approval_cannot_dispatch(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile, max_calls=1)
    approved = approve(engine, saved)
    args = execution(saved, approved, draft["steps"][0], key="unapproved")
    assert engine.call("daeri_capability_execute", args)["error"]["code"] == "SCOPE_VIOLATION"
    args["parameters"]["key"] = "orders"
    success = engine.call("daeri_capability_execute", args)["data"]
    args.update(call_id="call-2", expected_task_revision=success["task_revision"])
    assert engine.call("daeri_capability_execute", args)["error"]["code"] == "BUDGET_EXCEEDED"


def test_timeout_is_not_an_empty_success_and_same_call_never_retries(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile, max_runtime=500)
    approved = approve(engine, saved)
    args = execution(saved, approved, draft["steps"][0], key="slow")
    result = engine.call("daeri_capability_execute", args)
    assert result["error"]["code"] == "EXTERNAL_OUTCOME_UNKNOWN"
    assert result["data"]["call_state"] == "unknown"
    assert result["data"]["inline_result"] is None
    assert engine.call("daeri_capability_execute", args)["data"]["call_id"] == args["call_id"]


def table_plan(draft):
    draft['output'].update(format='CSV와 자연어 보고', table_rules=[], artifact_rules=[])


def comparison_report(first, second):
    refs = [first['result_ref'], second['result_ref']]
    return {'summary': 'The current values differ by three', 'proposed_task_state': 'succeeded',
        'findings': [{'kind': 'derived', 'text': 'Difference is three', 'evidence_refs': refs}],
        'checks': [{'criterion_id': 'read', 'status': 'met', 'method': 'Completed both approved reads', 'evidence_refs': refs}],
        'limitations': [], 'uncertain_effects': [], 'artifacts': [], 'next_actions': []}


def test_two_real_mcp_processes_report_verified_table_and_csv_with_replay(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile, configure=table_plan)
    approved = approve(engine, saved)
    first = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))["data"]
    second = engine.call("daeri_capability_execute", execution(saved, {**approved, "task_revision": first["task_revision"]}, draft["steps"][1], call_id="call-2"))["data"]
    args = {"request_id": "report", "task_id": saved["task_id"], "expected_task_revision": second["task_revision"], "plan_id": saved["plan_id"],
        "plan_version": saved["plan_version"], "report": comparison_report(first, second)}
    output = Path(engine.store.artifact_workspace(saved['task_id'])) / 'comparison.csv'
    output.write_text('ID,A,B,Difference\none,10,7,3\n')
    args['report']['artifacts'] = [{'artifact_id': 'csv', 'label': 'Comparison CSV',
        'local_path': str(output), 'media_type': 'text/csv',
        'source_refs': [first['result_ref'], second['result_ref']]}]
    stored = engine.call("daeri_result_submit", args)
    assert stored["status"] == "ok", stored
    assert stored["data"]["task_state"] == "succeeded"
    assert stored["data"]["validation"]["completion"] == "met"
    assert engine.call("daeri_result_submit", args)["data"] == stored["data"]
    artifact = engine.call("daeri_result_get", {"task_id": saved["task_id"], "record_ref": stored["data"]["artifact_refs"][0]})
    assert artifact["status"] == "ok", artifact
    assert Path(artifact["data"]["managed_file"]["local_path"]).read_text() == "ID,A,B,Difference\none,10,7,3\n"


def test_citing_and_reattaching_a_file_preserves_acyclic_sources(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile, configure=table_plan)
    approved = approve(engine, saved)
    first = engine.call('daeri_capability_execute', execution(saved, approved, draft['steps'][0]))['data']
    second = engine.call('daeri_capability_execute', execution(saved, {**approved, 'task_revision': first['task_revision']}, draft['steps'][1], call_id='call-2'))['data']
    report = comparison_report(first, second)
    path = Path(engine.store.artifact_workspace(saved['task_id'])) / 'answer.txt'
    path.write_text('Difference is three')
    report['artifacts'] = [{'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
                            'media_type': 'text/plain', 'source_refs': [first['result_ref'], second['result_ref']]}]
    args = {'request_id': 'first-report', 'task_id': saved['task_id'], 'expected_task_revision': second['task_revision'],
            'plan_id': saved['plan_id'], 'plan_version': saved['plan_version'], 'report': report}
    original = engine.call('daeri_result_submit', args)
    assert original['status'] == 'ok', original
    ref = original['data']['artifact_refs'][0]
    report['findings'] = [{'kind': 'derived', 'text': '이전에 작성한 파일 확인', 'evidence_refs': [ref]}]
    report['artifacts'] = [{'kind': 'existing', 'artifact_id': 'answer', 'label': '결과', 'artifact_ref': ref}]
    args.update(request_id='reattach', expected_task_revision=original['data']['task_revision'])
    attached = engine.call('daeri_result_submit', args)
    assert attached['status'] == 'ok', attached
    fetched = engine.call('daeri_result_get', {'task_id': saved['task_id'], 'record_ref': ref})
    assert fetched['status'] == 'ok', fetched
    assert engine.call('daeri_result_submit', args)['status'] == 'ok'


def test_cancellation_during_io_keeps_terminal_state_and_removes_unretained_late_data(configured):
    engine, profile = configured
    def no_retention(draft):
        for rule in draft["output"]["resource_rules"]:
            rule["retained_fields"] = []
    saved, draft = begin(engine, profile, configure=no_retention)
    approved = approve(engine, saved)
    args = execution(saved, approved, draft["steps"][0], key="slow")
    replies = []
    worker = threading.Thread(target=lambda: replies.append(engine.call("daeri_capability_execute", args)))
    worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = engine.store.get_task(saved["task_id"])
        if task["calls"] and task["calls"][0]["dispatched"]:
            break
        time.sleep(0.01)
    cancelled = engine.call("daeri_task_cancel", {"request_id": "stop", "task_id": saved["task_id"], "expected_task_revision": task["task_revision"]})
    assert cancelled["data"]["state"] == "cancelled"
    worker.join(timeout=10)
    assert not worker.is_alive()
    task = engine.store.get_task(saved["task_id"])
    assert task["state"] == "cancelled"
    body = engine.store.read_record(task["task_id"], task["calls"][0]["result_ref"])["payload"]
    assert body["result"] == {}


def test_output_policy_failure_preserves_completed_call_and_releases_reservation(configured):
    engine, profile = configured
    invalid_selector = {"segments": [{"key": "data"}, {"key": "rows"}, {"index": 0}, {"key": "amount"}, {"key": "nested"}]}
    def invalid_view(draft):
        draft["output"]["resource_rules"][0]["allowed_model_fields"] = [invalid_selector]
    saved, draft = begin(engine, profile, configure=invalid_view)
    approved = approve(engine, saved)
    args = execution(saved, approved, draft["steps"][0])
    result = engine.call("daeri_capability_execute", args)
    assert result["error"]["code"] == "RESULT_VALIDATION_FAILED"
    task = engine.store.get_task(saved["task_id"])
    assert task["calls"][0]["call_state"] == "completed"
    assert task["calls"][0]["result_ref"]
    assert task["reserved"]["runtime_ms"] == 0
    assert engine.call("daeri_capability_execute", args)["data"]["call_state"] == "completed"


def test_redaction_remains_visible_but_is_not_usable_as_original_evidence(configured):
    engine, profile = configured
    field = {"segments": [{"key": "data"}, {"key": "rows"}, {"each": True}, {"key": "amount"}]}
    def masked(draft):
        draft["output"]["resource_rules"][0]["masking"] = [{"field": field, "action": "redact"}]
    saved, draft = begin(engine, profile, configure=masked)
    approved = approve(engine, saved)
    result = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))
    task = engine.store.get_task(saved["task_id"])
    evidence = engine._load_evidence(task, result["data"]["result_ref"])
    assert evidence["result"]["data"]["rows"][0]["amount"] == "[REDACTED]"
    assert "/data/rows/0/amount" not in evidence["allowed_paths"]
    assert "/data/rows/0/amount" in evidence["provided_paths"]
    engine._finish(task, "partial")
    retained = engine.store.read_record(task["task_id"], result["data"]["result_ref"])["payload"]
    assert retained["result"]["data"]["rows"][0]["amount"] == "[REDACTED]"


def test_unknown_read_is_reported_as_unresolved_on_resume(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile, max_runtime=500)
    approved = approve(engine, saved)
    result = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0], key="slow"))
    resumed = engine.call("daeri_task_resume", {"request_id": "resume", "task_id": saved["task_id"], "expected_task_revision": result["data"]["task_revision"]})
    assert resumed["status"] == "pending"
    assert resumed["data"]["resumed"] is False
    assert resumed["data"]["unresolved_call_ids"] == ["call-1"]


def test_masked_binding_is_not_treated_as_proven_absence(configured):
    engine, profile = configured
    field = {"segments": [{"key": "data"}, {"key": "rows"}, {"each": True}, {"key": "amount"}]}
    def conditional(draft):
        draft["output"]["resource_rules"][0]["masking"] = [{"field": field, "action": "redact"}]
        draft["steps"][1].update(depends_on=["read-0"], input_bindings=[{
            "binding_id": "amount", "source_step_id": "read-0", "source_pointer": "/data/rows/0/amount", "target_pointer": None}],
            conditions=[{"binding_id": "amount", "operator": "exists"}])
    saved, draft = begin(engine, profile, configure=conditional)
    approved = approve(engine, saved)
    first = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))["data"]
    args = execution(saved, {**approved, "task_revision": first["task_revision"]}, draft["steps"][1], call_id="call-2")
    args["bindings"] = [{"binding_id": "amount", "source_call_id": "call-1"}]
    assert engine.call("daeri_capability_execute", args)["error"]["code"] == "BINDING_INVALID"


def test_dead_owner_dispatch_becomes_unknown_without_releasing_unsettled_budget(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    result = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))
    task = engine.store.get_task(saved["task_id"])
    task["owner_id"] = "999999:dead-test-owner"
    task["calls"][0].update(call_state="pending", result_ref=None, reserved_runtime_ms=500)
    task["reserved"]["runtime_ms"] = 500
    engine._save(task)
    resumed = engine.call("daeri_task_resume", {"request_id": "recover", "task_id": saved["task_id"], "expected_task_revision": task["task_revision"]})
    assert resumed["status"] == "pending", resumed
    recovered = engine.store.get_task(saved["task_id"])
    assert recovered["calls"][0]["call_state"] == "unknown"
    assert recovered["reserved"]["runtime_ms"] == 500
    assert recovered["usage"] == task["usage"]


def test_resumed_task_requires_fresh_approval_before_success_report(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile, configure=table_plan)
    approved = approve(engine, saved)
    first = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))["data"]
    second = engine.call("daeri_capability_execute", execution(saved, {**approved, "task_revision": first["task_revision"]}, draft["steps"][1], call_id="call-2"))["data"]
    paused = engine.call("daeri_task_update", {"request_id": "pause", "task_id": saved["task_id"], "expected_task_revision": second["task_revision"], "action": "pause"})["data"]
    resumed = engine.call("daeri_task_resume", {"request_id": "resume", "task_id": saved["task_id"], "expected_task_revision": paused["task_revision"]})["data"]
    result = engine.call("daeri_result_submit", {"request_id": "report", "task_id": saved["task_id"],
        "expected_task_revision": resumed["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "report": comparison_report(first, second)})
    assert result["error"]["code"] == "APPROVAL_REQUIRED"
    assert engine.store.get_task(saved["task_id"])["state"] == "awaiting_approval"


@pytest.mark.parametrize("replace_approved_plan", [False, True])
def test_partial_report_cannot_generate_files_from_unapproved_plan(configured, replace_approved_plan):
    engine, profile = configured
    def file_rule(draft):
        draft["output"]["format"] = "보고 파일"
    saved, draft = begin(engine, profile, configure=file_rule)
    if replace_approved_plan:
        approved = approve(engine, saved)
        search = engine.call("daeri_memory_search", {"task_id": saved["task_id"], "query": "replacement report"})["data"]
        draft["memory_search_refs"] = [search["memory_search_ref"]]
        draft["previous_plan_id"] = saved["plan_id"]
        draft["output"]["format"] = "수정 보고 파일"
        replacement = engine.call("daeri_plan_submit", {"request_id": "replacement", "task_id": saved["task_id"],
            "expected_task_revision": approved["task_revision"], "draft": draft})
        assert replacement["status"] == "ok", replacement
        saved = replacement["data"]
    report = {"summary": "No external reads completed", "proposed_task_state": "partial", "findings": [], "tables": [],
        "checks": [{"criterion_id": "read", "status": "unmet", "method": "Still waiting for approval", "evidence_refs": [], "reason": "Not executed"}],
        "limitations": ["Not executed"], "uncertain_effects": [], "next_actions": [],
        "artifacts": [{"artifact_id": "report-file", "label": "Unapproved file", "local_path": "/not-created.txt",
                       "media_type": "text/plain", "source_refs": []}]}
    result = engine.call("daeri_result_submit", {"request_id": "unapproved-file", "task_id": saved["task_id"],
        "expected_task_revision": saved["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "report": report})
    assert result["status"] == "error", result
    assert result["error"]["code"] == "APPROVAL_REQUIRED"
    assert engine.store.get_task(saved["task_id"])["state"] == "awaiting_approval"
    assert list((engine.store.data_dir / "artifacts").iterdir()) == []
