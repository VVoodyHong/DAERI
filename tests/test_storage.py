import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daeri.storage import Store, StoreError


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    yield instance
    instance.close()


def task_for(store, task_id="task-one", profile_name="alpha", created_at="2026-10-20T00:00:00+00:00"):
    profile = store.put_profile(profile_name)
    task = {
        "task_id": task_id,
        "profile_id": profile["profile_id"],
        "task_revision": 1,
        "state": "planning",
        "owner_id": "host-one",
        "context": {"profile_id": profile["profile_id"], "profile_revision": 1},
        "request_ref": None,
        "active_plan_ref": None,
        "plan_refs": [],
        "approval_requests": [],
        "waiting_reason": None,
        "created_at": created_at,
    }
    store.insert_task(task)
    return task


def test_profiles_persist_with_immutable_revisions(tmp_path):
    data = tmp_path / "data"
    first = Store(data)
    profile = first.put_profile("alpha", core_context=["운영 규칙"])
    assert profile.get("name") == "alpha"
    assert profile["revision"] == 1
    assert profile["retention_days"] is None
    assert profile["long_term_memory_days"] is None
    original = next((data / "profiles").glob("*.json"))
    original_bytes = original.read_bytes()
    revised = first.put_profile("alpha", core_context=["새 규칙"])
    assert revised["profile_id"] == profile["profile_id"]
    assert revised["revision"] == 2
    assert original.read_bytes() == original_bytes
    first.close()
    reopened = Store(data)
    try:
        assert reopened.get_profile(profile["profile_id"]) == revised
        assert reopened.list_profiles() == [revised]
    finally:
        reopened.close()


@pytest.mark.parametrize("name", ["", "../alpha", "alpha space", "a" * 65])
def test_profile_name_cannot_escape_managed_storage(store, name):
    with pytest.raises(StoreError, match="INVALID_INPUT"):
        store.put_profile(name)


@pytest.mark.parametrize("options", [
    {"retention_days": 0}, {"retention_days": True}, {"retention_days": -1}, {"retention_days": 1.5},
    {"long_term_memory_days": 0}, {"long_term_memory_days": True},
    {"long_term_memory_days": -1}, {"long_term_memory_days": 1.5},
    {"core_context": "plaintext"}, {"core_context": [42]},
    {"resources": [{"connection": {"password": "do-not-echo"}}]},
])
def test_invalid_policy_or_plaintext_credentials_are_rejected(store, options):
    with pytest.raises(StoreError, match="INVALID_INPUT") as caught:
        store.put_profile("alpha", **options)
    assert "do-not-echo" not in str(caught.value)
    assert store.list_profiles() == []


def test_unconnected_resources_are_not_silently_published(store):
    with pytest.raises(StoreError, match="INVALID_INPUT"):
        store.put_profile("alpha", resources=[{"auth_ref": "credential-one"}])


@pytest.mark.parametrize("days", [None, 90, 365])
def test_memory_retention_choice_survives_reopen_without_changing_body_retention(tmp_path, days):
    data = tmp_path / "retention"
    with closing(Store(data)) as store:
        profile = store.put_profile("service", long_term_memory_days=days)
    with closing(Store(data)) as reopened:
        assert reopened.get_profile(profile["profile_id"])["long_term_memory_days"] == days
        assert reopened.get_profile(profile["profile_id"])["retention_days"] is None


@pytest.mark.parametrize("days", [None, 15, 30])
def test_body_retention_choice_survives_reopen(tmp_path, days):
    with closing(Store(tmp_path)) as store:
        profile = store.put_profile("service", retention_days=days)
    with closing(Store(tmp_path)) as reopened:
        assert reopened.get_profile(profile["profile_id"])["retention_days"] == days


def test_unlimited_task_retention_never_removes_an_existing_finite_record_limit(store):
    task = task_for(store)
    limit = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    limited = store.write_record(task["task_id"], "report", {"summary": "finite"}, limit)
    unlimited = store.write_record(task["task_id"], "report", {"summary": "unlimited"})
    store.set_task_expiry(task["task_id"], None)
    assert store.read_record(task["task_id"], limited)["expires_at"] == limit
    assert store.read_record(task["task_id"], unlimited)["expires_at"] is None
    store.cleanup(now=datetime.now(UTC) + timedelta(days=3650))
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        store.read_record(task["task_id"], limited)
    assert store.read_record(task["task_id"], unlimited)["payload"] == {"summary": "unlimited"}


def test_edited_profile_blocks_reads_and_further_publication(store, tmp_path):
    store.put_profile("alpha")
    path = next((tmp_path / "data" / "profiles").glob("*.json"))
    path.write_text('{"name":"tampered"}')
    with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
        store.get_profile("alpha")
    with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
        store.put_profile("alpha")


def test_task_revision_is_atomic_across_connections(store, tmp_path):
    task = task_for(store)
    other = Store(tmp_path / "data")
    try:
        current = dict(task, state="awaiting_approval", task_revision=2)
        store.update_task(current, expected_revision=1)
        with pytest.raises(StoreError, match="REVISION_CONFLICT"):
            other.update_task(dict(task, state="cancelled", task_revision=2), expected_revision=1)
        assert other.get_task(task["task_id"])["state"] == "awaiting_approval"
        assert other.list_tasks(task["profile_id"]) == [current]
        assert other.list_tasks("unrelated-profile") == []
    finally:
        other.close()


def test_transaction_rolls_back_task_and_idempotency_together(store):
    task = task_for(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.update_task(dict(task, state="cancelled", task_revision=2), expected_revision=1)
            with store.transaction():
                store.request_put("task", "request-one", "fingerprint-one", {"task_id": task["task_id"]})
            raise RuntimeError("interrupted")
    assert store.get_task(task["task_id"])["task_revision"] == 1
    assert store.request_get("task", "request-one") is None


def test_duplicate_request_never_overwrites_original_response(store):
    store.request_put("create", "request-one", "fingerprint-one", {"task_id": "one"})
    store.request_put("create", "request-one", "fingerprint-one", {"task_id": "two"})
    with pytest.raises(StoreError, match="REQUEST_ID_CONFLICT"):
        store.request_put("create", "request-one", "different", {})
    assert store.request_get("create", "request-one") == {
        "fingerprint": "fingerprint-one", "response": {"task_id": "one"},
    }


def test_records_are_opaque_private_and_bound_to_their_task(store, tmp_path):
    task = task_for(store)
    ref = store.write_record(task["task_id"], "request", {"text": "sensitive request body"})
    assert "/" not in ref
    assert store.read_record(task["task_id"], ref) == {
        "record_kind": "request", "payload": {"text": "sensitive request body"}, "expires_at": None,
    }
    metadata = store.list_records(task["task_id"])
    assert metadata == [{"record_ref": ref, "record_kind": "request", "summary": "request"}]
    assert "sensitive request body" not in json.dumps(metadata)
    with pytest.raises(StoreError, match="NOT_FOUND"):
        store.read_record("different-task", ref)
    if os.name != "nt":
        for path in (tmp_path / "data").rglob("*"):
            assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("damage", ["overwrite", "delete"])
def test_missing_or_corrupt_body_never_returns_success(store, tmp_path, damage):
    task = task_for(store)
    ref = store.write_record(task["task_id"], "request", {"text": "original"})
    path = next((tmp_path / "data" / "records").glob("*.json"))
    if damage == "overwrite":
        path.write_text('{"text":"tampered"}')
    else:
        path.unlink()
    with pytest.raises(StoreError, match="RECORD_UNAVAILABLE"):
        store.read_record(task["task_id"], ref)


def test_expiry_blocks_before_cleanup_and_is_never_extended(store, tmp_path, monkeypatch):
    task = task_for(store)
    now = datetime.now(UTC)
    expired = (now - timedelta(days=1)).isoformat()
    ref = store.write_record(task["task_id"], "request", {"text": "expiring"})
    store.set_task_expiry(task["task_id"], expired)
    store.set_task_expiry(task["task_id"], (now + timedelta(days=30)).isoformat())
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        store.read_record(task["task_id"], ref)
    assert list((tmp_path / "data" / "records").glob("*.json"))
    original_unlink = Path.unlink
    def refuse_record(path, *args, **kwargs):
        if path.parent.name == "records":
            raise PermissionError("busy file")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", refuse_record)
    assert store.cleanup(now)["pending"] == 1
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        store.read_record(task["task_id"], ref)
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert store.cleanup(now)["pending"] == 0
    assert not list((tmp_path / "data" / "records").glob("*.json"))
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        store.read_record(task["task_id"], ref)
    assert store.get_task(task["task_id"])["task_revision"] == 1


def test_late_record_inherits_task_deadline(store):
    task = task_for(store)
    store.set_task_expiry(task["task_id"], "2000-01-01T00:00:00+00:00")
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        store.write_record(task["task_id"], "report", {"text": "late"})


def test_shared_store_serializes_threads(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: store.request_put("scope", str(i), str(i), {"index": i}), range(20)))
    assert store.request_get("scope", "19")["response"] == {"index": 19}


def test_sqlite_wal_and_fts5_are_available_without_body_indexing(store, tmp_path):
    task = task_for(store)
    store.write_record(task["task_id"], "request", {"text": "never-index-this-body"})
    with sqlite3.connect(tmp_path / "data" / "daeri.sqlite3") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        db.execute("CREATE VIRTUAL TABLE temp.search_check USING fts5(body)")
        tables = db.execute("SELECT name FROM sqlite_master WHERE sql LIKE '%USING fts5%'").fetchall()
        assert tables == []


def test_reopening_cleans_unpublished_files_after_rollback(tmp_path):
    data = tmp_path / "data"
    first = Store(data)
    task = task_for(first)
    with pytest.raises(RuntimeError):
        with first.transaction():
            ref = first.write_record(task["task_id"], "request", {"text": "unpublished"})
            raise RuntimeError("interrupted before publication")
    assert list((data / "records").glob("*.json"))
    with pytest.raises(StoreError, match="NOT_FOUND"):
        first.read_record(task["task_id"], ref)
    first.close()
    reopened = Store(data)
    try:
        assert not list((data / "records").glob("*.json"))
        assert reopened.get_task(task["task_id"])["state"] == "planning"
    finally:
        reopened.close()


def test_corrupt_registered_path_cannot_delete_outside_storage(store, tmp_path):
    task = task_for(store)
    ref = store.write_record(task["task_id"], "request", {"text": "expiring"})
    outside = tmp_path / "do-not-delete.json"
    outside.write_text("valuable unrelated file")
    with sqlite3.connect(tmp_path / "data" / "daeri.sqlite3") as db:
        db.execute("UPDATE records SET path=?, expires_at=? WHERE record_ref=?",
                   (str(outside), "2000-01-01T00:00:00.000000+00:00", ref))
    with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
        store.cleanup()
    assert outside.read_text() == "valuable unrelated file"


def test_concurrent_hosts_cannot_both_publish_the_same_revision(store, tmp_path):
    task = task_for(store)
    other = Store(tmp_path / "data")
    def publish(instance):
        try:
            instance.update_task(dict(task, task_revision=2, state="cancelled"), expected_revision=1)
            return "published"
        except StoreError as error:
            return error.code
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(publish, [store, other])) == ["REVISION_CONFLICT", "published"]
    finally:
        other.close()


def test_unresolved_profile_can_be_saved_then_bound_before_approval(store):
    task = {"task_id": "unresolved", "profile_id": None, "task_revision": 1,
            "state": "awaiting_input", "context": {"profile_id": None, "profile_name": None, "profile_revision": None}}
    store.insert_task(task)
    assert store.get_task("unresolved")["profile_id"] is None
    profile = store.put_profile("alpha")
    bound = dict(task, profile_id=profile["profile_id"], task_revision=2, state="planning")
    store.update_task(bound, expected_revision=1)
    assert store.list_tasks(profile["profile_id"]) == [bound]


def test_context_rebinding_updates_profile_filter_atomically(store):
    task = task_for(store)
    target = store.put_profile("beta")
    updated = dict(task, profile_id=target["profile_id"], task_revision=2)
    store.update_task(updated, expected_revision=1)
    assert store.list_tasks(task["profile_id"]) == []
    assert store.list_tasks(target["profile_id"]) == [updated]


def test_task_page_offset_returns_the_next_task_in_latest_first_order(store):
    first = task_for(store, "first")
    task_for(store, "second", created_at="2026-10-20T01:00:00+00:00")
    page = store.list_tasks(first["profile_id"], limit=1, offset=1)
    assert [task["task_id"] for task in page] == ["first"]


@pytest.mark.parametrize("offset", [-1, True, "1"])
def test_task_page_offset_requires_a_nonnegative_integer(store, offset):
    with pytest.raises(StoreError, match="INVALID_INPUT"):
        store.list_tasks("profile-one", offset=offset)


def test_updated_task_moves_to_the_front_of_recent_tasks(store):
    first = task_for(store, "first")
    task_for(store, "second", created_at="2026-10-20T01:00:00+00:00")
    assert [task["task_id"] for task in store.list_tasks(first["profile_id"])] == ["second", "first"]
    store.update_task(dict(first, task_revision=2, last_activity_at="2026-10-20T02:00:00+00:00"), expected_revision=1)
    assert [task["task_id"] for task in store.list_tasks(first["profile_id"])] == ["first", "second"]


def test_equal_recent_timestamps_sort_by_task_id(store):
    first = task_for(store, "alpha-task")
    task_for(store, "zeta-task")
    assert [task["task_id"] for task in store.list_tasks(first["profile_id"])] == ["alpha-task", "zeta-task"]


@pytest.mark.parametrize("name", ["daeri.sqlite3", "daeri.sqlite3-wal", "daeri.sqlite3-shm"])
def test_database_and_sidecar_symlinks_are_rejected_before_opening(tmp_path, name):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"")
    (data / name).symlink_to(outside)
    with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
        instance = Store(data)
        instance.close()
    assert outside.read_bytes() == b""


@pytest.mark.parametrize("damage", ["missing_db", "empty_db", "missing_profile_table", "missing_expiry_column"])
def test_broken_database_is_not_reinitialized_over_existing_records(tmp_path, damage):
    data = tmp_path / "data"
    instance = Store(data)
    task = task_for(instance)
    instance.write_record(task["task_id"], "request", {"text": "preserve until recovered"})
    instance.close()
    files = {path: path.read_bytes() for folder in ("records", "profiles") for path in (data / folder).glob("*.json")}
    database = data / "daeri.sqlite3"
    if damage == "missing_db":
        database.unlink()
    elif damage == "empty_db":
        database.write_bytes(b"")
    else:
        with sqlite3.connect(database) as db:
            db.execute("DROP TABLE profiles" if damage == "missing_profile_table" else "ALTER TABLE records DROP COLUMN expires_at")
    with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
        reopened = Store(data)
        reopened.close()
    assert {path: path.read_bytes() for path in files} == files


def test_simultaneous_initialization_keeps_both_hosts_usable(tmp_path):
    from threading import Barrier
    barrier = Barrier(2)
    def open_host(index):
        barrier.wait(timeout=5)
        instance = Store(tmp_path / "data")
        try:
            instance.request_put("host", str(index), str(index), {"host": index})
        finally:
            instance.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(open_host, [0, 1]))
    reopened = Store(tmp_path / "data")
    try:
        assert reopened.request_get("host", "0")["response"] == {"host": 0}
        assert reopened.request_get("host", "1")["response"] == {"host": 1}
    finally:
        reopened.close()


def test_ambiguous_profile_name_and_id_never_silently_select_a_different_target(store):
    original = store.put_profile("alpha")
    alias = store.put_profile(original["profile_id"])
    assert store.get_profile(alias["profile_id"])["name"] == original["profile_id"]
    with pytest.raises(StoreError, match="CONTEXT_REQUIRED"):
        store.get_profile(original["profile_id"])


def test_task_page_signature_changes_only_when_the_profiles_tasks_change(store):
    task = task_for(store)
    initial = store.task_page_signature(task["profile_id"])
    task_for(store, "other-profile-task", profile_name="beta")
    assert store.task_page_signature(task["profile_id"]) == initial
    store.update_task(dict(task, task_revision=2, state="paused"), expected_revision=1)
    revised = store.task_page_signature(task["profile_id"])
    assert revised != initial
    task_for(store, "second-alpha-task")
    assert store.task_page_signature(task["profile_id"]) != revised


@pytest.mark.parametrize("policy", [
    [], {"unknown": True}, {"categories": "lesson"},
    {"categories": ["unknown"]}, {"categories": ["lesson", "lesson"]},
    {"categories": [{}]}, {"instance_ids": "source-one"},
    {"instance_ids": ["source-one", "source-one"]}, {"instance_ids": [1]},
    {"instance_ids": ["unregistered"]}, {"target_bindings": []},
    {"target_bindings": {"unregistered": "0" * 64}},
    *({key: value} for key in ("model_allowed", "retain_allowed", "long_term_allowed", "exports_allowed")
      for value in ("true", 1, None, [])),
])
def test_invalid_profile_memory_policy_never_creates_a_revision(store, policy):
    original = store.put_profile("alpha")
    with pytest.raises(StoreError, match="INVALID_INPUT"):
        store.put_profile("alpha", memory_policy=policy)
    assert store.get_profile("alpha") == original


@pytest.mark.parametrize('outcome,expected', [('running', True), ('exited', False),
                                            ('missing', False), ('denied', True), ('unknown', True)])
def test_windows_process_probe_never_sends_a_signal_and_closes_handles(monkeypatch, outcome, expected):
    import sys
    from types import SimpleNamespace
    from daeri import storage
    closed = []
    def open_process(access, inherit, pid):
        assert (access, inherit, pid) == (0x100000, False, 123)
        if outcome in {'missing', 'denied'}:
            error = OSError('probe failed')
            error.winerror = 87 if outcome == 'missing' else 5
            raise error
        return 7
    api = SimpleNamespace(SYNCHRONIZE=0x100000, WAIT_OBJECT_0=0, WAIT_TIMEOUT=258,
                          OpenProcess=open_process, CloseHandle=closed.append,
                          WaitForSingleObject=lambda handle, timeout: {'running': 258, 'exited': 0, 'unknown': 999}[outcome])
    monkeypatch.setitem(sys.modules, '_winapi', api)
    monkeypatch.setattr(storage, 'sys', SimpleNamespace(platform='win32'), raising=False)
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('A Windows process probe must not send signals'))
    assert storage.process_alive(123) is expected
    assert closed == ([] if outcome in {'missing', 'denied'} else [7])


@pytest.mark.parametrize('error,expected', [(None, True), (ProcessLookupError(), False), (PermissionError(), True)])
def test_posix_process_probe_distinguishes_dead_from_uncertain_owners(monkeypatch, error, expected):
    from types import SimpleNamespace
    from daeri import storage
    def probe(pid, signal):
        assert (pid, signal) == (123, 0)
        if error:
            raise error
    monkeypatch.setattr(storage, 'sys', SimpleNamespace(platform='darwin'), raising=False)
    monkeypatch.setattr(os, 'kill', probe)
    assert storage.process_alive(123) is expected


@pytest.mark.parametrize('pid', [0, -1, 0x100000000, True])
def test_invalid_process_ids_never_target_a_process_or_process_group(monkeypatch, pid):
    from daeri.storage import process_alive
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('Invalid owners cannot be probed'))
    with pytest.raises(ValueError):
        process_alive(pid)
