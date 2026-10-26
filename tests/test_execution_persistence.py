"""Post-dispatch storage outages keep one observable call and settled local usage."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from daeri.core.errors import StoreError
from test_execution_flow import configured, begin, approve, execution, comparison_report
from test_subagents import create_subagent, run_read


@pytest.mark.parametrize("delegated", [False, True])
def test_result_write_failure_keeps_original_receipt_and_allows_fresh_read_completion(configured, monkeypatch, delegated):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"] if delegated else None
    args = execution(saved, approved, draft["steps"][0])
    write_record = runtime.store.write_record

    def fail_result(task_id, kind, payload, **kwargs):
        if kind == "call_result" and "intent" not in payload:
            raise StoreError("STORE_UNAVAILABLE", "Result file cannot be written.")
        return write_record(task_id, kind, payload, **kwargs)

    clock = iter([1.0, 1.125])
    monkeypatch.setattr("daeri.application.runtime.monotonic", lambda: next(clock, 1.125))
    monkeypatch.setattr(runtime.store, "write_record", fail_result)
    invoke = lambda: run_read(runtime, saved, grant) if delegated else runtime.call("daeri_capability_execute", args)
    failed = invoke()
    assert failed["error"]["code"] == "RESULT_PERSIST_FAILED", failed
    monkeypatch.setattr(runtime.store, "write_record", write_record)

    task = runtime.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
    call_id = "subagent-read" if delegated else "call-1"
    assert task["active_calls"] == []
    replayed = invoke()
    assert replayed["error"]["code"] == "RESULT_PERSIST_FAILED", replayed
    assert replayed["data"]["result_ref"] is None
    assert replayed["data"]["call_id"] == call_id
    assert replayed["data"]["call_state"] == "failed"
    stored = runtime.store.get_task(saved["task_id"])
    assert stored["usage"]["tool_calls"] == 1
    assert stored["usage"]["runtime_ms"] == 125
    assert stored["reserved"]["runtime_ms"] == 0
    results = []
    for step_id in ("read-0", "read-1"):
        observed = runtime.call("daeri_capability_execute", {
            "task_id": saved["task_id"], "step_id": step_id, "parameters": {"key": "orders"}})
        assert observed["status"] == "ok", observed
        results.append(observed["data"])
    assert len(runtime.store.get_task(saved["task_id"])["calls"]) == 3
    resumed = runtime.call("daeri_task_resume", {"task_id": saved["task_id"]})
    assert resumed["data"]["unresolved_call_ids"] == []
    ended = runtime.call("daeri_result_submit", {"task_id": saved["task_id"],
        "report": comparison_report(*results)})
    assert ended["status"] == "ok", ended
    assert ended["data"]["task_state"] == "succeeded", ended


def test_store_recovery_flushes_stopped_call_while_its_executor_is_alive(configured, monkeypatch):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    args = execution(saved, approved, draft["steps"][0])
    execute, get_task = runtime.registry.execute, runtime.store.get_task
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

    monkeypatch.setattr(runtime.registry, "execute", lose_store)
    monkeypatch.setattr(runtime.store, "get_task", guarded_task)
    clock = iter([1.0, 1.25])
    monkeypatch.setattr("daeri.application.runtime.monotonic", lambda: next(clock, 1.25))
    failed = runtime.call("daeri_capability_execute", args)
    assert failed["error"]["code"] == "RESULT_PERSIST_FAILED", failed
    unavailable = False
    replayed = runtime.call("daeri_capability_execute", args)
    assert replayed["data"]["call_state"] == "failed", replayed
    stored = runtime.store.get_task(saved["task_id"])
    assert stored["usage"]["tool_calls"] == 1
    assert stored["usage"]["runtime_ms"] == 250
    assert stored["reserved"]["runtime_ms"] == 0
    monkeypatch.setattr(runtime.registry, "execute", execute)
    observed = runtime.call("daeri_capability_execute", {
        "task_id": saved["task_id"], "step_id": "read-0", "parameters": {"key": "orders"}})
    assert observed["status"] == "ok", observed


def test_revoked_subagent_still_settles_usage_when_its_late_result_cannot_be_written(configured, monkeypatch):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    execute, write_record = runtime.registry.execute, runtime.store.write_record

    def revoke_during_read(invocation, **kwargs):
        task = runtime.store.get_task(saved["task_id"])
        revoked = runtime.call("daeri_subagent", {"operation": "revoke", "request_id": "revoke-during-read",
            "task_id": task["task_id"], "expected_task_revision": task["task_revision"],
            "delegation_id": grant["delegation_id"]})
        assert revoked["status"] == "ok", revoked
        return execute(invocation, **kwargs)

    def fail_result(task_id, kind, payload, **kwargs):
        if kind == "call_result" and "intent" not in payload:
            raise StoreError("STORE_UNAVAILABLE", "Result file cannot be written.")
        return write_record(task_id, kind, payload, **kwargs)

    monkeypatch.setattr(runtime.registry, "execute", revoke_during_read)
    monkeypatch.setattr(runtime.store, "write_record", fail_result)
    clock = iter([1.0, 1.5])
    monkeypatch.setattr("daeri.application.runtime.monotonic", lambda: next(clock))
    denied = run_read(runtime, saved, grant)
    assert denied["error"]["code"] == "SUBAGENT_UNAVAILABLE", denied
    task = runtime.store.get_task(saved["task_id"])
    assert task["delegations"][0]["state"] == "revoked"
    assert task["state"] == "running"
    assert task["calls"][0]["call_state"] == "failed"
    assert task["calls"][0]["error_code"] == "RESULT_PERSIST_FAILED"
    assert task["usage"]["runtime_ms"] == 500
    assert task["usage"]["tool_calls"] == 1
    assert task["reserved"]["runtime_ms"] == 0


def test_resume_does_not_mark_a_live_in_flight_call_as_storage_failure(configured, monkeypatch):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    started, release = threading.Event(), threading.Event()
    execute = runtime.registry.execute

    def wait_for_release(invocation, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return execute(invocation, **kwargs)

    monkeypatch.setattr(runtime.registry, "execute", wait_for_release)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.call, "daeri_capability_execute", execution(saved, approved, draft["steps"][0]))
        try:
            assert started.wait(timeout=5)
            task = runtime.store.get_task(saved["task_id"])
            resumed = runtime.call("daeri_task_resume", {"request_id": "while-running", "task_id": task["task_id"],
                "expected_task_revision": task["task_revision"]})
            assert resumed["status"] == "pending", resumed
            active = runtime.store.get_task(saved["task_id"])["calls"][0]
            assert active["call_state"] == "pending"
            assert active["error_code"] is None
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "ok"


def test_failed_dispatch_commit_releases_the_intent_without_transmitting(configured, monkeypatch):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    args = execution(saved, approved, draft["steps"][0])
    update_task = runtime.store.update_task

    def fail_dispatch(task, revision):
        if task["calls"] and task["calls"][0]["dispatched"]:
            raise StoreError("STORE_UNAVAILABLE", "Dispatch metadata cannot be committed.")
        return update_task(task, revision)

    monkeypatch.setattr(runtime.store, "update_task", fail_dispatch)
    failed = runtime.call("daeri_capability_execute", args)
    assert failed["error"]["code"] == "STORE_UNAVAILABLE", failed
    monkeypatch.setattr(runtime.store, "update_task", update_task)
    replayed = runtime.call("daeri_capability_execute", args)
    assert replayed["data"]["call_state"] == "blocked", replayed
    task = runtime.store.get_task(saved["task_id"])
    assert task["calls"][0]["dispatched"] is False
    assert task["usage"]["tool_calls"] == 0
    assert task["reserved"]["tool_calls"] == 0
    assert task["reserved"]["runtime_ms"] == 0
