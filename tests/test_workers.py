"""Scoped host workers share one approval, execution ledger and profile memory."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import subprocess
import sys
import threading

import pytest

from daeri.engine import Engine
from test_execution_flow import configured, begin, approve, execution


def create_worker(engine, saved, step_ids, *, key="worker", **extra):
    revision = engine.store.get_task(saved["task_id"])["task_revision"]
    return engine.call("daeri_worker", {
        "operation": "create", "request_id": key, "task_id": saved["task_id"],
        "expected_task_revision": revision,
        "assignment": {"label": "조회 작업자", "objective": "승인된 조회를 확인한다",
            "step_ids": step_ids, "completion_criteria": ["출처와 확인 한계를 보고한다"], "source_refs": []},
        **extra,
    })


def worker_call(engine, saved, grant, operation, **extra):
    return engine.call("daeri_worker", {"operation": operation, "task_id": saved["task_id"],
        "delegation_token": grant["delegation_token"], **extra})


def run_read(engine, saved, grant, step="read-0", call_id="worker-read", **extra):
    return worker_call(engine, saved, grant, "execute", call={
        "step_id": step, "call_id": call_id, "parameters": {"key": "orders"}}, **extra)


def test_worker_creation_requires_real_root_approval_and_registered_steps(configured):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    assert create_worker(engine, saved, ["read-0"])["error"]["code"] == "APPROVAL_REQUIRED"
    approve(engine, saved)
    assert create_worker(engine, saved, ["invented"])["error"]["code"] == "SCOPE_VIOLATION"
    grant = create_worker(engine, saved, ["read-0"])
    assert grant["status"] == "ok", grant
    assert [step["step_id"] for step in grant["data"]["steps"]] == ["read-0"]
    assert len(grant["data"]["contracts"]) == 1


def test_worker_on_another_engine_executes_only_assigned_step_and_replays(configured):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"]
    worker = Engine(engine.store.data_dir)
    try:
        denied = run_read(worker, saved, grant, step="read-1")
        assert denied["error"]["code"] == "SCOPE_VIOLATION"
        first = run_read(worker, saved, grant)
        assert first["status"] == "ok", first
        assert first["data"]["inline_result"]["data"]["rows"][0]["amount"] == "10.00"
        assert run_read(worker, saved, grant)["data"]["result_ref"] == first["data"]["result_ref"]
        assert engine.store.get_task(saved["task_id"])["usage"]["tool_calls"] == 1
        grant["delegation_token"] = "invented"
        assert run_read(worker, saved, grant)["error"]["code"] == "ACCESS_DENIED"
    finally:
        worker.close()


def test_active_worker_steps_have_one_assignment_and_coordinator_cannot_overlap(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    create_worker(engine, saved, ["read-0"])
    assert create_worker(engine, saved, ["read-0"], key="overlap")["error"]["code"] == "WORKER_CONFLICT"
    args = execution(saved, approved, draft["steps"][0])
    args["expected_task_revision"] = engine.store.get_task(saved["task_id"])["task_revision"]
    assert engine.call("daeri_capability_execute", args)["error"]["code"] == "WORKER_CONFLICT"


def test_parallel_workers_spend_one_shared_budget_without_revision_races(configured):
    engine, profile = configured
    saved, _ = begin(engine, profile, max_calls=2, max_runtime=30000)
    approve(engine, saved)
    a = create_worker(engine, saved, ["read-0"], key="a")["data"]
    b = create_worker(engine, saved, ["read-1"], key="b")["data"]
    workers = [Engine(engine.store.data_dir), Engine(engine.store.data_dir)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_read, workers[0], saved, a, "read-0", "a-read"),
                       pool.submit(run_read, workers[1], saved, b, "read-1", "b-read")]
            outcomes = [future.result(timeout=15) for future in futures]
        assert [result["status"] for result in outcomes] == ["ok", "ok"], outcomes
        task = engine.store.get_task(saved["task_id"])
        assert task["usage"]["tool_calls"] == 2
        assert task["reserved"]["runtime_ms"] == 0
        assert run_read(workers[0], saved, a, call_id="extra")["error"]["code"] == "BUDGET_EXCEEDED"
    finally:
        for worker in workers:
            worker.close()


def test_worker_reads_only_assigned_or_own_evidence_and_completes_with_provenance(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    other = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][1]))["data"]
    grant = create_worker(engine, saved, ["read-0"])["data"]
    own = run_read(engine, saved, grant)["data"]
    assert worker_call(engine, saved, grant, "read", record_ref=other["result_ref"])["error"]["code"] == "ACCESS_DENIED"
    assert worker_call(engine, saved, grant, "read", record_ref=own["result_ref"])["status"] == "ok"
    result = {"summary": "조회 완료", "evidence_refs": [own["result_ref"]], "limitations": []}
    completed = worker_call(engine, saved, grant, "complete", request_id="done", result=result)
    assert completed["status"] == "ok", completed
    assert completed["data"]["state"] == "completed"
    assert completed["data"]["result"] == result
    loaded = engine.call("daeri_result_get", {"task_id": saved["task_id"],
        "record_ref": completed["data"]["result_ref"]})
    assert loaded["data"]["payload"] == result
    assert worker_call(engine, saved, grant, "complete", request_id="done", result=result)["data"]["result_ref"] == completed["data"]["result_ref"]
    assert run_read(engine, saved, grant, call_id="after-complete")["error"]["code"] == "WORKER_UNAVAILABLE"
    task = engine.store.get_task(saved["task_id"])
    assert task["state"] == "running"
    assert task["last_report_ref"] is None


def test_coordinator_can_recover_worker_metadata_without_exposing_tokens(configured):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"]
    fetched = engine.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
    assert fetched["workers"][0]["delegation_id"] == grant["delegation_id"]
    assert fetched["workers"][0]["step_ids"] == ["read-0"]
    assert "delegation_token" not in fetched["workers"][0]
    assert "token_hash" not in fetched["workers"][0]


def test_explicit_source_handoff_can_cross_assigned_execution_instances(configured):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    prior = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][1]))["data"]
    args = {"operation": "create", "request_id": "handoff", "task_id": saved["task_id"],
        "expected_task_revision": prior["task_revision"], "assignment": {"label": "비교", "objective": "이전 근거와 비교",
            "step_ids": ["read-0"], "completion_criteria": ["비교 근거 반환"], "source_refs": [prior["result_ref"]]}}
    grant = engine.call("daeri_worker", args)
    assert grant["status"] == "ok", grant
    assert worker_call(engine, saved, grant["data"], "read", record_ref=prior["result_ref"])["status"] == "ok"
    assert run_read(engine, saved, grant["data"], step="read-1")["error"]["code"] == "SCOPE_VIOLATION"


def test_worker_summary_cannot_restore_source_fields_removed_at_task_end(configured):
    engine, profile = configured
    def transient(draft):
        draft["output"]["resource_rules"][0]["retained_fields"] = []
    saved, draft = begin(engine, profile, configure=transient)
    approved = approve(engine, saved)
    other = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][1]))["data"]
    grant = create_worker(engine, saved, ["read-0"])["data"]
    own = run_read(engine, saved, grant)["data"]
    completed = worker_call(engine, saved, grant, "complete", request_id="done", result={
        "summary": "조회 값은 10.00이다", "evidence_refs": [own["result_ref"]], "limitations": []})["data"]
    report = {"summary": "두 조회 완료", "proposed_task_state": "succeeded", "findings": [], "tables": [],
        "checks": [{"criterion_id": "read", "status": "met", "method": "승인된 조회 확인",
            "evidence_refs": [own["result_ref"], other["result_ref"]]}],
        "limitations": [], "uncertain_effects": [], "artifacts": [], "next_actions": []}
    task = engine.store.get_task(saved["task_id"])
    assert engine.call("daeri_result_submit", {"request_id": "report", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], **grant["plan_ref"], "report": report})["status"] == "ok"
    loaded = engine.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": completed["result_ref"]})
    assert loaded["error"]["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("dispatched", [True, False])
@pytest.mark.parametrize("terminal", [False, True])
def test_root_resume_recovers_a_dead_worker_without_losing_shared_accounting(configured, dispatched, terminal):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"]
    child = """
import json, os, sys
from daeri.engine import Engine
p = json.load(sys.stdin)
engine = Engine(p['directory'])
if p['dispatched']:
    engine.registry.execute = lambda *a, **kw: os._exit(0)
else:
    original = engine._execution_allowed
    checks = 0
    def allowed(*a, **kw):
        global checks
        checks += 1
        if checks == 2:
            os._exit(0)
        return original(*a, **kw)
    engine._execution_allowed = allowed
engine.call('daeri_worker', {'operation':'execute', 'task_id':p['task_id'], 'delegation_token':p['token'],
    'call':{'step_id':'read-0', 'call_id':'crashed', 'parameters':{'key':'orders'}}})
"""
    crashed = subprocess.run([sys.executable, "-c", child], input=json.dumps({
        "directory": str(engine.store.data_dir), "task_id": saved["task_id"],
        "token": grant["delegation_token"], "dispatched": dispatched}), text=True, capture_output=True, timeout=10)
    assert crashed.returncode == 0, crashed.stderr
    task = engine.store.get_task(saved["task_id"])
    assert task["calls"][0]["call_state"] == "pending"
    if terminal:
        report = {"summary": "작업자 중단", "proposed_task_state": "partial", "findings": [], "tables": [],
            "checks": [{"criterion_id": "read", "status": "unknown", "method": "작업자 중단으로 확인 필요",
                "evidence_refs": [], "reason": "작업자 실행 프로세스 종료"}], "limitations": ["작업자 중단으로 조회 완료 확인 필요"],
            "uncertain_effects": engine._uncertain(task), "artifacts": [], "next_actions": []}
        ended = engine.call("daeri_result_submit", {"request_id": "partial", "task_id": task["task_id"],
            "expected_task_revision": task["task_revision"], **grant["plan_ref"], "report": report})
        assert ended["status"] == "ok", ended
        task = engine.store.get_task(saved["task_id"])
        assert task["state"] == "partial"
    result = engine.call("daeri_task_resume", {"request_id": "recover-worker", "task_id": saved["task_id"],
        "expected_task_revision": task["task_revision"]})
    assert result["status"] in {"ok", "pending"}, result
    task = engine.store.get_task(saved["task_id"])
    assert task["calls"][0]["call_state"] == ("unknown" if dispatched else "blocked")
    assert task["reserved"]["runtime_ms"] == (10000 if dispatched else 0)
    assert task["usage"]["tool_calls"] == int(dispatched)


@pytest.mark.parametrize("action", ["pause", "cancel", "revoke", "expire"])
def test_worker_authority_ends_with_root_control_or_expiry(configured, action):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"]
    task = engine.store.get_task(saved["task_id"])
    if action == "pause":
        engine.call("daeri_task_update", {"request_id": "pause", "task_id": saved["task_id"],
            "expected_task_revision": task["task_revision"], "action": "pause"})
    elif action == "cancel":
        engine.call("daeri_task_cancel", {"request_id": "cancel", "task_id": saved["task_id"],
            "expected_task_revision": task["task_revision"]})
    elif action == "revoke":
        result = engine.call("daeri_worker", {"operation": "revoke", "request_id": "revoke",
            "task_id": saved["task_id"], "expected_task_revision": task["task_revision"],
            "delegation_id": grant["delegation_id"]})
        assert result["data"]["state"] == "revoked"
    else:
        with engine.store.transaction():
            task["delegations"][0]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            engine._save(task)
    assert run_read(engine, saved, grant)["error"]["code"] == "WORKER_UNAVAILABLE"
    assert engine.store.get_task(saved["task_id"])["calls"] == []


def test_worker_search_uses_shared_profile_memory_but_scopes_instances(configured):
    engine, profile = configured
    profile = engine.store.put_profile("worker-search", resources=profile["resources"],
        core_context=["운영 용어"], embedding={"provider": "keyword_only"}, memory_policy={"categories": ["procedure"],
            "instance_ids": ["cache-a"], "model_allowed": True, "retain_allowed": True})
    for resource in profile["resources"]:
        engine.registry.diagnose(profile, resource["instance_id"])
    saved, _ = begin(engine, profile)
    approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"]
    result = worker_call(engine, saved, grant, "search", query="집계 방법")
    assert result["status"] in {"ok", "partial"}, result
    assert result["data"]["core_context"] == ["운영 용어"]
    denied = worker_call(engine, saved, grant, "search", query="집계 방법", instance_ids=["cache-b"])
    assert denied["error"]["code"] == "SCOPE_VIOLATION"


def test_revoke_during_io_preserves_late_result_and_releases_shared_reservation(configured, monkeypatch):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"]
    worker = Engine(engine.store.data_dir)
    started, release = threading.Event(), threading.Event()
    real_execute = worker.registry.execute
    def waiting(invocation, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return real_execute(invocation, **kwargs)
    monkeypatch.setattr(worker.registry, "execute", waiting)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_read, worker, saved, grant)
            assert started.wait(timeout=5)
            task = engine.store.get_task(saved["task_id"])
            revoked = engine.call("daeri_worker", {"operation": "revoke", "request_id": "stop-worker",
                "task_id": task["task_id"], "expected_task_revision": task["task_revision"],
                "delegation_id": grant["delegation_id"]})
            assert revoked["status"] == "ok", revoked
            assert create_worker(engine, saved, ["read-0"], key="premature")["error"]["code"] == "WORKER_CONFLICT"
            release.set()
            assert future.result(timeout=10)["error"]["code"] == "WORKER_UNAVAILABLE"
        task = engine.store.get_task(saved["task_id"])
        assert task["calls"][0]["call_state"] == "completed"
        assert task["calls"][0]["result_ref"]
        assert task["reserved"]["runtime_ms"] == 0
        assert task["usage"]["tool_calls"] == 1
        assert create_worker(engine, saved, ["read-0"], key="replacement")["status"] == "ok"
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize("extra", [{"expected_task_revision": 1}, {"assignment": {}}, {"operation": "approve"}])
def test_worker_gateway_rejects_mixed_operations(configured, extra):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    payload = {"operation": "get", "task_id": saved["task_id"], "delegation_token": "token", **extra}
    assert engine.call("daeri_worker", payload)["error"]["code"] == "INVALID_INPUT"
