import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "data")
    yield instance
    instance.close()


@pytest.mark.parametrize('fail_commit', [False, True])
def test_transaction_guard_restores_side_effects_before_releasing_database(store, fail_commit):
    connection = store._db
    events = []

    class FailingCommit:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            raise sqlite3.OperationalError('COMMIT_FAILED')

    @contextmanager
    def guard():
        assert connection.in_transaction
        events.append('entered')
        try:
            yield
        except BaseException:
            assert connection.in_transaction
            events.append('restored')
            raise

    if fail_commit:
        store._db = FailingCommit()
    try:
        with pytest.raises(StoreError if fail_commit else RuntimeError):
            with store.transaction(guard=guard()):
                store.put_profile('guarded')
                if not fail_commit:
                    raise RuntimeError('CALLER_FAILED')
    finally:
        store._db = connection
    assert events == ['entered', 'restored']
    assert not connection.in_transaction
    assert store.list_profiles() == []


def test_transaction_guard_requires_its_own_commit_boundary(store):
    @contextmanager
    def guard():
        pytest.fail('Nested side effects must be rejected before entry.')
        yield

    with store.transaction():
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            with store.transaction(guard=guard()):
                pass


@pytest.mark.parametrize('change', ['move', 'move_recreate', 'replace_database', 'replace_database_set', 'delete_database',
                                  'root_symlink', 'database_symlink', 'root_file', 'database_directory'])
def test_changed_store_location_blocks_cached_reads_and_closes_the_old_connection(store, tmp_path, change):
    store.request_put('test', 'old-key', 'old-fingerprint', {'old': True})
    connection = store._db
    data = store.data_dir
    moved = tmp_path / 'moved'
    database = data / 'daeri.sqlite3'
    replacement = None
    if change in {'move', 'move_recreate', 'root_symlink', 'root_file'}:
        data.rename(moved)
        if change == 'move_recreate':
            replacement = Store(data)
            replacement.put_profile('new-profile')
        elif change == 'root_symlink':
            data.symlink_to(moved, target_is_directory=True)
        elif change == 'root_file':
            data.write_text('different object')
    elif change == 'replace_database':
        replacement = Store(tmp_path / 'replacement')
        replacement.request_put('test', 'new-key', 'new-fingerprint', {'new': True})
        replacement.close()
        replacement = None
        candidate = tmp_path / 'replacement' / 'daeri.sqlite3'
        candidate.replace(database)
        replacement_bytes = database.read_bytes()
    elif change == 'replace_database_set':
        moved.mkdir()
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = data / ('daeri.sqlite3' + suffix)
            if path.exists():
                path.rename(moved / path.name)
        replacement = Store(data)
        replacement.put_profile('new-profile')
        replacement_files = {path: path.read_bytes() for path in (database, data / 'daeri.sqlite3-wal')}
    elif change == 'delete_database':
        database.unlink()
    else:
        original = data / 'original.sqlite3'
        database.rename(original)
        if change == 'database_symlink':
            database.symlink_to(original)
        else:
            database.mkdir()
    try:
        with pytest.raises(StoreError) as error:
            store.request_get('test', 'old-key')
        assert error.value.code == 'STORE_UNAVAILABLE'
        assert '연결' in error.value.message
        assert str(tmp_path) not in str(error.value)
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')
        store.close()
        store.close()
        if change == 'replace_database_set':
            assert {path: path.read_bytes() for path in replacement_files} == replacement_files
        if replacement is not None:
            replacement.put_profile('still-usable')
            assert [profile['name'] for profile in replacement.list_profiles()] == ['new-profile', 'still-usable']
        if change == 'replace_database':
            assert database.read_bytes() == replacement_bytes
    finally:
        if replacement is not None:
            replacement.close()


def test_restoring_the_original_location_requires_a_new_store_connection(store, tmp_path):
    store.request_put('test', 'old-key', 'old-fingerprint', {'old': True})
    data, moved = store.data_dir, tmp_path / 'moved'
    data.rename(moved)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        store.assert_current_location()
    moved.rename(data)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        store.request_get('test', 'old-key')
    reopened = Store(data)
    try:
        assert reopened.request_get('test', 'old-key')['response'] == {'old': True}
    finally:
        reopened.close()


def test_unverifiable_store_location_is_permanently_closed(store, monkeypatch):
    store.request_put('test', 'old-key', 'old-fingerprint', {'old': True})
    original = Path.lstat

    def denied(path):
        if path == store.data_dir:
            raise PermissionError('private path must not be returned')
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', denied)
        with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
            store.request_get('test', 'old-key')
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        store.request_get('test', 'old-key')
    with pytest.raises(sqlite3.ProgrammingError):
        store._db.execute('SELECT 1')


@pytest.mark.parametrize('nested', [False, True])
def test_location_change_rolls_back_after_the_transaction_guard_restores_side_effects(store, tmp_path, nested):
    connection, events = store._db, []
    store.request_put('test', 'old-key', 'old-fingerprint', {'old': True})
    moved = tmp_path / 'moved'

    @contextmanager
    def guard():
        try:
            yield
        except StoreError:
            events.append(('guard', connection.in_transaction))
            raise

    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        with store.transaction(guard=guard()):
            store.request_put('test', 'uncommitted', 'pending-fingerprint', {'pending': True})
            store.data_dir.rename(moved)
            if nested:
                with store.transaction():
                    pytest.fail('A nested transaction must fail before executing its body.')
    assert events == [('guard', True)]
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute('SELECT 1')
    reopened = Store(moved)
    try:
        assert reopened.request_get('test', 'uncommitted') is None
        assert reopened.request_get('test', 'old-key')['response'] == {'old': True}
    finally:
        reopened.close()


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


@pytest.mark.parametrize('field', ['retention_days', 'long_term_memory_days'])
@pytest.mark.parametrize('days', [3_000_000, 10 ** 100])
def test_unrepresentable_retention_is_rejected_without_replacing_profile(store, field, days):
    previous = store.put_profile('alpha', retention_days=30, long_term_memory_days=365)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        store.put_profile('alpha', **{field: days})
    assert store.get_profile('alpha') == previous


@pytest.mark.parametrize('field', ['retention_days', 'long_term_memory_days'])
def test_legacy_unrepresentable_retention_returns_a_configuration_error(store, field):
    profile = store.put_profile('alpha')
    profile[field] = 10 ** 100
    content = json.dumps(profile).encode()
    row = store._db.execute('SELECT path FROM profiles WHERE name=?', ('alpha',)).fetchone()
    (store.data_dir / row['path']).write_bytes(content)
    store._db.execute('UPDATE profiles SET size=?,sha256=? WHERE name=?',
                      (len(content), hashlib.sha256(content).hexdigest(), 'alpha'))
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        store.get_profile('alpha')


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


def test_memory_job_updates_preserve_task_revision_deadline_and_cleanup_references(store):
    task = task_for(store)
    expiry = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    original = {'report_ref': 'report-one', 'items': [{'state': 'pending'}]}
    ref = store.write_record(task['task_id'], 'memory_pending', original, expiry)
    before = dict(store._db.execute('SELECT * FROM records WHERE record_ref=?', (ref,)).fetchone())
    pending = {'report_ref': 'report-one', 'items': [{'state': 'pending', 'memory_ref': 'memory-one'}]}
    store.write_memory_job(task['task_id'], ref, pending)
    done = {'report_ref': 'report-one', 'items': [{'state': 'ready', 'memory_ref': 'memory-one'}]}
    store.write_memory_job(task['task_id'], ref, done, done=True)
    after = dict(store._db.execute('SELECT * FROM records WHERE record_ref=?', (ref,)).fetchone())
    assert store.read_record(task['task_id'], ref) == {
        'record_kind': 'memory_done', 'payload': done, 'expires_at': before['expires_at']}
    assert store.get_task(task['task_id']) == task
    assert before['path'] != after['path'] and before['expires_at'] == after['expires_at']
    tracked = {row[0] for row in store._db.execute(
        "SELECT reference FROM profile_references WHERE profile_id=? AND kind='file'", (task['profile_id'],))}
    assert {before['path'], after['path']} <= tracked
    store.write_memory_job(task['task_id'], ref, done, done=True)
    assert dict(store._db.execute('SELECT * FROM records WHERE record_ref=?', (ref,)).fetchone()) == after
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        store.write_memory_job(task['task_id'], ref, pending)


@pytest.mark.parametrize('defect', ['other_kind', 'other_task', 'report_changed', 'expired', 'damaged', 'prepare_failed'])
def test_memory_job_publication_keeps_original_record_on_invalid_or_unavailable_updates(store, monkeypatch, defect):
    task = task_for(store)
    value = {'report_ref': 'report-one', 'items': [{'state': 'pending'}]}
    ref = store.write_record(task['task_id'], 'request' if defect == 'other_kind' else 'memory_pending', value)
    before = dict(store._db.execute('SELECT * FROM records WHERE record_ref=?', (ref,)).fetchone())
    task_id = task['task_id']
    next_value = {'report_ref': 'report-one', 'items': [{'state': 'ready'}]}
    code = 'INVALID_INPUT'
    if defect == 'other_task':
        task_for(store, 'other-task', 'beta')
        task_id, code = 'other-task', 'NOT_FOUND'
    elif defect == 'report_changed':
        next_value['report_ref'] = 'report-two'
    elif defect == 'expired':
        store.set_task_expiry(task['task_id'], (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        before = dict(store._db.execute('SELECT * FROM records WHERE record_ref=?', (ref,)).fetchone())
        code = 'RECORD_EXPIRED'
    elif defect == 'damaged':
        (store.data_dir / before['path']).write_text('{}', encoding='utf-8')
        code = 'RECORD_UNAVAILABLE'
    elif defect == 'prepare_failed':
        def unavailable(*args):
            raise StoreError('STORE_UNAVAILABLE', 'test storage failure')
        monkeypatch.setattr(store, '_prepare', unavailable)
        code = 'STORE_UNAVAILABLE'
    with pytest.raises(StoreError, match=code):
        store.write_memory_job(task_id, ref, next_value, done=True)
    assert dict(store._db.execute('SELECT * FROM records WHERE record_ref=?', (ref,)).fetchone()) == before


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


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX special files')
@pytest.mark.parametrize('kind,expected', [
    ('profile', 'STORE_UNAVAILABLE'), ('record', 'RECORD_UNAVAILABLE'),
    ('artifact', 'RECORD_UNAVAILABLE'), ('workspace', 'ready'),
])
def test_replaced_special_files_never_block_storage_reads_or_startup(store, kind, expected):
    task = task_for(store)
    if kind == 'profile':
        path = store.data_dir / store._db.execute('SELECT path FROM profiles').fetchone()[0]
        reference = ''
    elif kind == 'record':
        reference = store.write_record(task['task_id'], 'request', {'text': 'original'})
        path = store.data_dir / store._db.execute('SELECT path FROM records').fetchone()[0]
    elif kind == 'artifact':
        reference = store.write_artifact(task['task_id'], b'original', 'text', 'text/plain', 'txt')
        path = Path(store.artifact_file(task['task_id'], reference)['local_path'])
    else:
        path = Path(store.artifact_workspace(task['task_id'])) / 'answer.txt'
        path.write_bytes(b'original')
        reference = store.import_artifact(task['task_id'], str(path), 'text/plain')
    path.unlink()
    os.mkfifo(path)
    script = '''
from pathlib import Path
import sys
from daeri.infrastructure.storage import Store, StoreError
store = Store(Path(sys.argv[1]))
try:
    if sys.argv[2] == 'profile':
        store.get_profile('alpha')
    elif sys.argv[2] == 'record':
        store.read_record('task-one', sys.argv[3])
    elif sys.argv[2] == 'artifact':
        store.artifact_file('task-one', sys.argv[3])
except StoreError as error:
    print(error.code)
else:
    print('ready')
finally:
    store.close()
'''
    result = subprocess.run([sys.executable, '-c', script, str(store.data_dir), kind, reference],
                            capture_output=True, text=True, timeout=3, check=True)
    assert result.stdout.strip() == expected
    assert path.exists()


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


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX special files')
@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_sqlite_special_files_are_rejected_without_blocking_or_replacing_them(tmp_path, suffix):
    data = tmp_path / 'data'
    instance = Store(data)
    instance.put_profile('keep', core_context=['preserve'])
    instance.close()
    path = data / ('daeri.sqlite3' + suffix)
    path.unlink(missing_ok=True)
    os.mkfifo(path)
    script = '''
from pathlib import Path
import sys
from daeri.infrastructure.storage import Store, StoreError
try:
    store = Store(Path(sys.argv[1]))
except StoreError as error:
    print(error.code)
else:
    store.close()
    print('opened')
'''
    result = subprocess.run([sys.executable, '-c', script, str(data)],
                            capture_output=True, text=True, timeout=3, check=True)
    assert result.stdout.strip() == 'STORE_UNAVAILABLE'
    assert path.exists() and not path.is_file()
    assert 'preserve' in next((data / 'profiles').glob('*.json')).read_text()


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
    import daeri.infrastructure.storage as storage
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
    import daeri.infrastructure.storage as storage
    def probe(pid, signal):
        assert (pid, signal) == (123, 0)
        if error:
            raise error
    monkeypatch.setattr(storage, 'sys', SimpleNamespace(platform='darwin'), raising=False)
    monkeypatch.setattr(os, 'kill', probe)
    assert storage.process_alive(123) is expected


@pytest.mark.parametrize('pid', [0, -1, 0x100000000, True])
def test_invalid_process_ids_never_target_a_process_or_process_group(monkeypatch, pid):
    from daeri.infrastructure.storage import process_alive
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('Invalid owners cannot be probed'))
    with pytest.raises(ValueError):
        process_alive(pid)
