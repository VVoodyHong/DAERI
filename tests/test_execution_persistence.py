"""Post-dispatch storage outages keep one observable call and settled local usage."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from daeri.storage import StoreError
from test_execution_flow import configured, begin, approve, execution
from test_workers import create_worker, run_read


@pytest.mark.parametrize("delegated", [False, True])
def test_result_write_failure_is_unresolved_without_repeating_dispatch(configured, monkeypatch, delegated):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"] if delegated else None
    args = execution(saved, approved, draft["steps"][0])
    write_record = engine.store.write_record

    def fail_result(task_id, kind, payload, **kwargs):
        if kind == "call_result" and "intent" not in payload:
            raise StoreError("STORE_UNAVAILABLE", "Result file cannot be written.")
        return write_record(task_id, kind, payload, **kwargs)

    clock = iter([1.0, 1.125])
    monkeypatch.setattr("daeri.engine.monotonic", lambda: next(clock, 1.125))
    monkeypatch.setattr(engine.store, "write_record", fail_result)
    invoke = lambda: run_read(engine, saved, grant) if delegated else engine.call("daeri_capability_execute", args)
    failed = invoke()
    assert failed["error"]["code"] == "RESULT_PERSIST_FAILED", failed
    monkeypatch.setattr(engine.store, "write_record", write_record)

    task = engine.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
    call_id = "worker-read" if delegated else "call-1"
    assert task["active_calls"][0]["call_id"] == call_id
    assert task["active_calls"][0]["call_state"] == "unknown"
    replayed = invoke()
    assert replayed["error"]["code"] == "RESULT_PERSIST_FAILED", replayed
    assert replayed["data"]["result_ref"] is None
    stored = engine.store.get_task(saved["task_id"])
    assert stored["usage"]["tool_calls"] == 1
    assert stored["usage"]["runtime_ms"] == 125
    assert stored["reserved"]["runtime_ms"] == 0
    fresh = execution(saved, {**approved, "task_revision": stored["task_revision"]}, draft["steps"][1], call_id="after-loss")
    blocked = engine.call("daeri_capability_execute", fresh)
    assert blocked["status"] == "error", blocked
    assert blocked["error"]["code"] == "RESULT_PERSIST_FAILED", blocked
    assert len(engine.store.get_task(saved["task_id"])["calls"]) == 1
    resumed = engine.call("daeri_task_resume", {"request_id": "recover-result", "task_id": saved["task_id"],
        "expected_task_revision": stored["task_revision"]})
    assert resumed["data"]["unresolved_call_ids"] == [call_id]
    report = {"summary": "조회 응답의 보존 실패", "proposed_task_state": "partial", "findings": [], "tables": [],
        "checks": [{"criterion_id": "read", "status": "unknown", "method": "원 호출 상태 확인",
            "evidence_refs": [], "reason": "조회 결과를 보존하지 못함"}], "limitations": ["조회 결과 보존 실패"],
        "uncertain_effects": [], "artifacts": [], "next_actions": []}
    task = engine.store.get_task(saved["task_id"])
    ended = engine.call("daeri_result_submit", {"request_id": "partial", "task_id": saved["task_id"],
        "expected_task_revision": task["task_revision"], "plan_id": saved["plan_id"],
        "plan_version": saved["plan_version"], "report": report})
    assert ended["data"]["task_state"] == "partial", ended


def test_store_recovery_flushes_stopped_call_while_its_executor_is_alive(configured, monkeypatch):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    args = execution(saved, approved, draft["steps"][0])
    execute, get_task = engine.registry.execute, engine.store.get_task
    unavailable = False

    def lose_store(invocation, **kwargs):
        nonlocal unavailable
        result = execute(invocation, **kwargs)
        unavailable = True
        return result

    def guarded_task(task_id):
        if unavailable:
            raise StoreError("STORE_UNAVAILABLE", "Store is temporarily unavailable.")
        return get_task(task_id)

    monkeypatch.setattr(engine.registry, "execute", lose_store)
    monkeypatch.setattr(engine.store, "get_task", guarded_task)
    clock = iter([1.0, 1.25])
    monkeypatch.setattr("daeri.engine.monotonic", lambda: next(clock))
    failed = engine.call("daeri_capability_execute", args)
    assert failed["error"]["code"] == "RESULT_PERSIST_FAILED", failed
    unavailable = False
    replayed = engine.call("daeri_capability_execute", args)
    assert replayed["data"]["call_state"] == "unknown", replayed
    stored = engine.store.get_task(saved["task_id"])
    assert stored["usage"]["tool_calls"] == 1
    assert stored["usage"]["runtime_ms"] == 250
    assert stored["reserved"]["runtime_ms"] == 0


def test_revoked_worker_still_settles_usage_when_its_late_result_cannot_be_written(configured, monkeypatch):
    engine, profile = configured
    saved, _ = begin(engine, profile)
    approve(engine, saved)
    grant = create_worker(engine, saved, ["read-0"])["data"]
    execute, write_record = engine.registry.execute, engine.store.write_record

    def revoke_during_read(invocation, **kwargs):
        task = engine.store.get_task(saved["task_id"])
        revoked = engine.call("daeri_worker", {"operation": "revoke", "request_id": "revoke-during-read",
            "task_id": task["task_id"], "expected_task_revision": task["task_revision"],
            "delegation_id": grant["delegation_id"]})
        assert revoked["status"] == "ok", revoked
        return execute(invocation, **kwargs)

    def fail_result(task_id, kind, payload, **kwargs):
        if kind == "call_result" and "intent" not in payload:
            raise StoreError("STORE_UNAVAILABLE", "Result file cannot be written.")
        return write_record(task_id, kind, payload, **kwargs)

    monkeypatch.setattr(engine.registry, "execute", revoke_during_read)
    monkeypatch.setattr(engine.store, "write_record", fail_result)
    clock = iter([1.0, 1.5])
    monkeypatch.setattr("daeri.engine.monotonic", lambda: next(clock))
    denied = run_read(engine, saved, grant)
    assert denied["error"]["code"] == "WORKER_UNAVAILABLE", denied
    task = engine.store.get_task(saved["task_id"])
    assert task["delegations"][0]["state"] == "revoked"
    assert task["state"] == "running"
    assert task["calls"][0]["call_state"] == "unknown"
    assert task["calls"][0]["error_code"] == "RESULT_PERSIST_FAILED"
    assert task["usage"]["runtime_ms"] == 500
    assert task["usage"]["tool_calls"] == 1
    assert task["reserved"]["runtime_ms"] == 0


def test_resume_does_not_mark_a_live_in_flight_call_as_storage_failure(configured, monkeypatch):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    started, release = threading.Event(), threading.Event()
    execute = engine.registry.execute

    def wait_for_release(invocation, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return execute(invocation, **kwargs)

    monkeypatch.setattr(engine.registry, "execute", wait_for_release)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine.call, "daeri_capability_execute", execution(saved, approved, draft["steps"][0]))
        try:
            assert started.wait(timeout=5)
            task = engine.store.get_task(saved["task_id"])
            resumed = engine.call("daeri_task_resume", {"request_id": "while-running", "task_id": task["task_id"],
                "expected_task_revision": task["task_revision"]})
            assert resumed["status"] == "pending", resumed
            active = engine.store.get_task(saved["task_id"])["calls"][0]
            assert active["call_state"] == "pending"
            assert active["error_code"] is None
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "ok"


def test_failed_dispatch_commit_releases_the_intent_without_transmitting(configured, monkeypatch):
    engine, profile = configured
    saved, draft = begin(engine, profile)
    approved = approve(engine, saved)
    args = execution(saved, approved, draft["steps"][0])
    update_task = engine.store.update_task

    def fail_dispatch(task, revision):
        if task["calls"] and task["calls"][0]["dispatched"]:
            raise StoreError("STORE_UNAVAILABLE", "Dispatch metadata cannot be committed.")
        return update_task(task, revision)

    monkeypatch.setattr(engine.store, "update_task", fail_dispatch)
    failed = engine.call("daeri_capability_execute", args)
    assert failed["error"]["code"] == "STORE_UNAVAILABLE", failed
    monkeypatch.setattr(engine.store, "update_task", update_task)
    replayed = engine.call("daeri_capability_execute", args)
    assert replayed["data"]["call_state"] == "blocked", replayed
    task = engine.store.get_task(saved["task_id"])
    assert task["calls"][0]["dispatched"] is False
    assert task["usage"]["tool_calls"] == 0
    assert task["reserved"]["tool_calls"] == 0
    assert task["reserved"]["runtime_ms"] == 0
