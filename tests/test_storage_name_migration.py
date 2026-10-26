"""Existing local history and encrypted backups keep their original evidence after renaming."""

import copy
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from daeri.config.portable_archive import open_bundle, write_bundle
from daeri.config.profile_portability import backup_profile, inspect_backup, restore_profile
from daeri.application.runtime import Runtime
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError
from test_profile_bundle_validation import CONTENT, DIGEST, HASH, manifest, validate
from test_storage import task_for


def test_open_migrates_legacy_record_and_request_metadata_without_rewriting_evidence(tmp_path):
    data = tmp_path / 'data'
    with closing(Store(data)) as store:
        task = task_for(store)
        assignment = store.write_record(task['task_id'], 'subagent_assignment', {'objective': 'Read the evidence'})
        result = store.write_record(task['task_id'], 'subagent_result', {'summary': 'Observed data', 'evidence_refs': [assignment]})
        rows = [dict(row) for row in store._db.execute('SELECT * FROM records')]
        original = {row['path']: (data / row['path']).read_bytes() for row in rows}
        store._db.execute("UPDATE records SET kind=replace(kind,'subagent_','worker_')")
        scope = 'worker:' + task['task_id']
        store.request_put(scope, 'create', 'fingerprint', {'delegation_id': 'delegation_one'}, profile_id=task['profile_id'])
        reference = json.dumps([scope, 'create'], separators=(',', ':'))
        deletion = {'references': [['request', reference]], 'shared': [['request', reference]]}
        store._db.execute("INSERT INTO profile_deletions VALUES(?,'deleting',?)", ('old-deletion', json.dumps(deletion)))
    for _ in range(2):
        with closing(Store(data)) as store:
            assert store.read_record(task['task_id'], assignment)['record_kind'] == 'subagent_assignment'
            assert store.read_record(task['task_id'], result)['record_kind'] == 'subagent_result'
            assert store.get_task(task['task_id']) == task
            assert store.request_get('subagent:' + task['task_id'], 'create') == {
                'fingerprint': 'fingerprint', 'response': {'delegation_id': 'delegation_one'}}
            assert store.request_get(scope, 'create') is None
            expected = json.dumps(['subagent:' + task['task_id'], 'create'], separators=(',', ':'))
            references = [row[0] for row in store._db.execute("SELECT reference FROM profile_references WHERE kind='request'")]
            assert references == [expected]
            migrated = json.loads(store._db.execute("SELECT manifest FROM profile_deletions WHERE profile_id='old-deletion'").fetchone()[0])
            assert migrated == {'references': [['request', expected]], 'shared': [['request', expected]]}
            for row in rows:
                current = store._db.execute('SELECT path,size,sha256 FROM records WHERE record_ref=?', (row['record_ref'],)).fetchone()
                assert tuple(current) == (row['path'], row['size'], row['sha256'])
                assert (data / row['path']).read_bytes() == original[row['path']]
    from daeri.config.setup import delete_profile, preview_profile_deletion
    with closing(Store(data)) as store:
        preview = preview_profile_deletion(store, task['profile_id'])
        delete_profile(store, task['profile_id'], preview['deletion_token'], credentials=object())
        assert store._db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
        assert store._db.execute('SELECT count(*) FROM profile_references').fetchone()[0] == 0


@pytest.mark.parametrize('identical', [True, False])
def test_request_namespace_migration_merges_only_identical_cached_requests(tmp_path, identical):
    with closing(Store(tmp_path)) as store:
        store.request_put('worker:task-one', 'create', 'original', {'delegation_id': 'one'})
        store.request_put('subagent:task-one', 'create', 'original' if identical else 'different', {'delegation_id': 'one'})
    if identical:
        with closing(Store(tmp_path)) as store:
            assert store._db.execute('SELECT count(*) FROM requests').fetchone()[0] == 1
            assert store.request_get('subagent:task-one', 'create')['fingerprint'] == 'original'
    else:
        with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
            Store(tmp_path)
        with closing(sqlite3.connect(tmp_path / 'daeri.sqlite3')) as database:
            assert database.execute('SELECT count(*) FROM requests').fetchone()[0] == 2


def legacy_manifest():
    value = manifest()
    blobs = {'blobs/' + DIGEST: CONTENT}
    for ref, kind, payload in (
        ('record_assignment', 'worker_assignment', {'objective': 'Read the evidence'}),
        ('record_result', 'worker_result', {'summary': 'Observed data', 'evidence_refs': ['record_one'],
                                         '_source_paths': {'record_one': ['/request_text']}, 'limitations': []}),
    ):
        content = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(content).hexdigest()
        value['records'].append({'record_ref': ref, 'task_id': 'task_one', 'kind': kind,
                                 'size': len(content), 'sha256': digest, 'expires_at': None,
                                 'blocked': 0, 'cleaned': 0, 'blob': 'blobs/' + digest})
        blobs['blobs/' + digest] = content
    value['tasks'][0]['metadata']['delegations'] = [{'delegation_id': 'delegation_one',
        'assignment_ref': 'record_assignment', 'result_ref': 'record_result', 'state': 'completed'}]
    value['requests'] = [{'scope': 'worker:task_one', 'request_key': 'create', 'fingerprint': HASH}]
    return value, blobs


def test_legacy_archive_restores_current_names_with_original_hashes_and_provenance(tmp_path):
    value, content = legacy_manifest()
    before = copy.deepcopy(value)
    assert validate(value, list(content.items())) is None
    assert value == before
    sources = {}
    for index, (name, body) in enumerate(content.items()):
        path = tmp_path / str(index)
        path.write_bytes(body)
        sources[name] = path
    archive = tmp_path / 'legacy.daeri'
    write_bundle(archive, value, sources, 'migration-test-password')
    preview = inspect_backup(archive, 'migration-test-password')
    expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert preview['confirmation_token'] == expected
    data = tmp_path / 'restored'
    with closing(Store(data)) as store:
        restore_profile(store, archive, 'migration-test-password', preview['confirmation_token'])
        for row in value['records']:
            current = store._db.execute('SELECT * FROM records WHERE record_ref=?', (row['record_ref'],)).fetchone()
            assert current['sha256'] == row['sha256'] and current['size'] == row['size']
            assert (data / current['path']).read_bytes() == content[row['blob']]
        assert store.read_record('task_one', 'record_result')['record_kind'] == 'subagent_result'
        with pytest.raises(StoreError, match='CONTEXT_CHANGED'):
            store.request_get('subagent:task_one', 'create')
        assert store.request_get('worker:task_one', 'create') is None
    runtime = Runtime(data)
    try:
        result = runtime.call('daeri_result_get', {'task_id': 'task_one', 'record_ref': 'record_result'})
        assert result['status'] == 'ok', result
        assert result['data']['record_kind'] == 'subagent_result'
        assert result['data']['evidence_refs'] == ['record_one']
        current = tmp_path / 'current.daeri'
        backup_profile(runtime, 'alpha', current, 'migration-test-password')
        with open_bundle(current, 'migration-test-password') as (packed, _):
            assert {row['kind'] for row in packed['records']} == {'request', 'subagent_assignment', 'subagent_result'}
            assert [row['scope'] for row in packed['requests']] == ['subagent:task_one']
    finally:
        runtime.close()


def test_legacy_archive_request_scope_still_requires_its_own_task():
    value = manifest()
    value['requests'] = [{'scope': 'worker:foreign_task', 'request_key': 'create', 'fingerprint': HASH}]
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


def test_legacy_archive_request_scopes_cannot_duplicate_the_current_namespace():
    value = manifest()
    value['requests'] = [{'scope': scope, 'request_key': 'create', 'fingerprint': HASH}
                         for scope in ('worker:task_one', 'subagent:task_one')]
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)
