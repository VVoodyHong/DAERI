import copy
import hashlib
import json
import os
from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest

from daeri.engine import Engine
from daeri.storage import Store, StoreError
from daeri.portable_archive import open_bundle
from daeri.profile_portability import backup_profile, inspect_backup, restore_profile
from test_memory_flow import remembered


PASSWORD = 'test-only-portability-password'


def test_encrypted_roundtrip_preserves_history_files_memory_and_isolates_profiles(remembered, tmp_path):
    engine, profile, saved, draft, approved, first, second, report = remembered
    other = engine.store.put_profile('unrelated-source', core_context=['OTHER_PROFILE_SECRET'])
    archive = tmp_path / 'profile.daeri'
    result = backup_profile(engine, profile['profile_id'], archive, PASSWORD)
    assert result['profile_id'] == profile['profile_id']
    assert b'OTHER_PROFILE_SECRET' not in archive.read_bytes()
    preview = inspect_backup(archive, PASSWORD)
    assert preview['counts']['tasks'] == 1
    with open_bundle(archive, PASSWORD) as (manifest, packed):
        assert manifest['profile']['profile_id'] != other['profile_id']
        assert manifest['memory_versions']
        assert all(row['blob'] is None for row in manifest['records'] if row['kind'] in {'memory_pending', 'memory_done'})
    destination = tmp_path / 'destination'
    with_store = Store(destination)
    unrelated = with_store.put_profile('unrelated-destination', core_context=['KEEP'])
    restored = restore_profile(with_store, archive, PASSWORD, preview['confirmation_token'])
    assert restored['profile_id'] == profile['profile_id']
    assert with_store.get_profile(unrelated['profile_id']) == unrelated
    old_task = engine.store.get_task(saved['task_id'])
    imported = with_store.get_task(saved['task_id'])
    assert imported['state'] == old_task['state'] == 'succeeded'
    assert imported['owner_id'] is None
    assert all(item['state'] != 'approved' for item in imported['approvals'])
    assert all(item.get('historical_ever_approved') and not item.get('ever_approved') for item in imported['approvals'] if item.get('approval_id'))
    assert with_store.read_record(saved['task_id'], first['result_ref'])['payload'] == engine.store.read_record(saved['task_id'], first['result_ref'])['payload']
    ref = report['artifact_refs'][0]
    assert Path(with_store.artifact_file(saved['task_id'], ref)['local_path']).read_bytes() == Path(engine.store.artifact_file(saved['task_id'], ref)['local_path']).read_bytes()
    assert with_store._db.execute('SELECT count(*) FROM memory_vectors').fetchone()[0] == 0
    with_store.close()
    target = Engine(destination)
    try:
        refs = {row[0] for row in target.store._db.execute('SELECT memory_ref FROM memory_fts')}
        assert refs
        recalled = target.memory.search(profile['profile_id'], 'Cache 비교', limit=5)
        assert recalled['items']
        assert target.call('daeri_result_get', {'task_id': saved['task_id'], 'record_ref': report['report_ref']})['status'] == 'ok'
    finally:
        target.close()


def test_wrong_password_confirmation_collision_and_tombstone_preserve_destination(tmp_path):
    source = Engine(tmp_path / 'source')
    profile = source.store.put_profile('portable')
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    source.close()
    store = Store(tmp_path / 'target')
    with pytest.raises(StoreError):
        restore_profile(store, archive, 'incorrect-password', 'x' * 64)
    preview = inspect_backup(archive, PASSWORD)
    with pytest.raises(StoreError, match='preview'):
        restore_profile(store, archive, PASSWORD, '0' * 64)
    assert store.list_profiles() == []
    store._db.execute('INSERT INTO profile_deletions VALUES(?,?,?)', (profile['profile_id'], 'deleted', '{}'))
    with pytest.raises(StoreError):
        restore_profile(store, archive, PASSWORD, preview['confirmation_token'])
    assert store.list_profiles() == []
    store._db.execute('DELETE FROM profile_deletions')
    store.put_profile('portable', core_context=['KEEP'])
    with pytest.raises(StoreError):
        restore_profile(store, archive, PASSWORD, preview['confirmation_token'])
    assert store.get_profile('portable')['core_context'] == ['KEEP']
    store.close()


def test_restore_failure_rolls_back_all_rows_and_removes_its_files(tmp_path, monkeypatch):
    source = Engine(tmp_path / 'source')
    profile = source.store.put_profile('portable')
    created = source.call('daeri_task_create', {'request_id': 'create', 'request_text': 'hello', 'context': {'profile_id': profile['profile_id']}})
    assert created['status'] == 'ok'
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    source.close()
    preview = inspect_backup(archive, PASSWORD)
    target = Store(tmp_path / 'target')
    original = target._prepare_bytes
    calls = []
    def fail(folder, content, extension):
        calls.append(folder)
        if len(calls) == 2:
            raise StoreError('STORE_UNAVAILABLE', 'injected failure')
        return original(folder, content, extension)
    monkeypatch.setattr(target, '_prepare_bytes', fail)
    with pytest.raises(StoreError):
        restore_profile(target, archive, PASSWORD, preview['confirmation_token'])
    assert target.list_profiles() == []
    assert target._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert list((target.data_dir / 'profiles').iterdir()) == []
    target.close()


def test_current_export_restriction_prevents_leaking_derived_prose(remembered, tmp_path):
    engine, profile, saved, *_ = remembered
    config = {key: copy.deepcopy(value) for key, value in profile.items() if key not in {'profile_id', 'revision'}}
    config['resources'][0]['policy']['export_fields'] = []
    engine.store.put_profile(**config)
    archive = tmp_path / 'restricted.daeri'
    with pytest.raises(StoreError) as rejected:
        backup_profile(engine, profile['profile_id'], archive, PASSWORD)
    assert rejected.value.code == 'ACCESS_DENIED'
    assert not archive.exists()


def test_expiry_during_storage_preserves_deadline_and_never_restores_body(remembered, tmp_path):
    engine, profile, saved, *_ = remembered
    archive = tmp_path / 'expired.daeri'
    expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    engine.store.set_task_expiry(saved['task_id'], expired)
    engine.store.cleanup()
    backup_profile(engine, profile['profile_id'], archive, PASSWORD)
    preview = inspect_backup(archive, PASSWORD)
    target = Store(tmp_path / 'target')
    restore_profile(target, archive, PASSWORD, preview['confirmation_token'])
    rows = target._db.execute('SELECT * FROM records').fetchall()
    assert rows and all(row['blocked'] == row['cleaned'] == 1 for row in rows)
    assert all(row['expires_at'] == expired for row in rows)
    assert list((target.data_dir / 'records').iterdir()) == []
    target.close()


def test_fresh_credentials_do_not_inherit_target_values_and_restore_never_calls_api(tmp_path, monkeypatch):
    from test_execution_flow import resource
    source = Engine(tmp_path / 'source')
    item = resource()
    item['connection']['auth_env'] = {'REDIS_PWD': 'credential:old-cache'}
    profile = source.store.put_profile('portable', resources=[item], embedding={
        'provider': 'api', 'adapter': 'openai_compatible', 'endpoint': 'https://example.test/v1/embeddings',
        'model': 'embedding-example', 'dimensions': 3, 'credential_ref': 'credential:old-embedding'})
    created = source.call('daeri_task_create', {'request_id': 'create', 'request_text': 'hello', 'context': {'profile_id': profile['profile_id']}})
    assert created['status'] == 'ok', created
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    source.close()
    def forbidden(*args, **kwargs):
        pytest.fail('Restore must not resolve credentials, call APIs or load local models')
    monkeypatch.setattr('daeri.memory.create_api_embedder', forbidden)
    monkeypatch.setattr('daeri.memory.LocalEmbedder.load', forbidden)
    monkeypatch.setattr('daeri.credentials.Credentials.get', forbidden)
    target = Store(tmp_path / 'target')
    result = restore_profile(target, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    current = target.get_profile('portable')
    assert len(result['credential_references']) == 2
    assert current['embedding']['credential_ref'] != 'credential:old-embedding'
    assert current['resources'][0]['connection']['auth_env']['REDIS_PWD'] != 'credential:old-cache'
    task = target.get_task(created['data']['task_id'])
    assert task['profile_snapshot']['resources'] == current['resources']
    assert task['profile_snapshot']['embedding'] == current['embedding']
    assert task['state'] == 'paused'
    target.close()


def test_worker_token_and_approved_request_cannot_be_replayed_after_restore(tmp_path):
    from test_execution_flow import resource, begin, approve, execution
    from test_workers import create_worker
    source = Engine(tmp_path / 'source')
    profile = source.store.put_profile('portable', resources=[resource()])
    source.registry.diagnose(profile, 'cache-a')
    saved, draft = begin(source, profile)
    approved = approve(source, saved)
    grant = create_worker(source, saved, ['read-0'])['data']
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    with open_bundle(archive, PASSWORD) as (manifest, packed):
        serialized = json.dumps(manifest)
        assert grant['delegation_token'] not in serialized
        assert hashlib.sha256(grant['delegation_token'].encode()).hexdigest() not in serialized
        assert all('response' not in item for item in manifest['requests'])
    source.close()
    store = Store(tmp_path / 'target')
    restore_profile(store, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    store.close()
    target = Engine(tmp_path / 'target')
    try:
        task = target.store.get_task(saved['task_id'])
        assert task['state'] == 'paused'
        restored = target.call('daeri_task_resume', {'request_id': 'restore-resume', 'task_id': saved['task_id'],
            'expected_task_revision': task['task_revision']})
        assert restored['status'] in {'ok', 'pending'}, restored
        assert target.store.get_task(saved['task_id'])['state'] in {'planning', 'awaiting_approval'}
        denied = target.call('daeri_worker', {'operation': 'get', 'task_id': saved['task_id'],
            'delegation_token': grant['delegation_token']})
        assert denied['error']['code'] == 'ACCESS_DENIED', denied
        args = execution(saved, approved, draft['steps'][0])
        args['expected_task_revision'] = target.store.get_task(saved['task_id'])['task_revision']
        assert target.call('daeri_capability_execute', args)['status'] == 'error'
        assert target.store.get_task(saved['task_id'])['calls'] == []
    finally:
        target.close()


def test_newly_expired_snapshot_does_not_extend_original_deadline(tmp_path, monkeypatch):
    source = Engine(tmp_path / 'source')
    profile = source.store.put_profile('portable')
    created = source.call('daeri_task_create', {'request_id': 'create', 'request_text': 'expiring', 'context': {'profile_id': profile['profile_id']}})['data']
    deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    source.store.set_task_expiry(created['task_id'], deadline)
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    source.close()
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr('daeri.profile_portability.datetime', Later)
    target = Store(tmp_path / 'target')
    restore_profile(target, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    row = target._db.execute('SELECT * FROM records').fetchone()
    assert row['expires_at'] == deadline and row['blocked'] == row['cleaned'] == 1
    assert not target._path(row['path'], 'records').exists()
    target.close()


def test_invalidated_memory_and_blocked_lineage_remain_unavailable(remembered, tmp_path):
    engine, profile, saved, *_ = remembered
    row = engine.store._db.execute('SELECT * FROM memory_versions').fetchone()
    lineage = {'keys': json.loads(row['source_lineage'])['keys'], 'blocked_at': datetime.now(UTC).isoformat()}
    engine.store._db.execute('UPDATE memory_heads SET invalidated=1')
    engine.store._db.execute('UPDATE memory_versions SET source_lineage=?', (json.dumps(lineage),))
    archive = tmp_path / 'profile.daeri'
    backup_profile(engine, profile['profile_id'], archive, PASSWORD)
    target = Store(tmp_path / 'target')
    restore_profile(target, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    assert target._db.execute('SELECT invalidated FROM memory_heads').fetchone()[0] == 1
    restored = target._db.execute('SELECT body,source_lineage FROM memory_versions').fetchone()
    assert restored['body'] is None
    assert json.loads(restored['source_lineage']) == lineage
    assert target._db.execute('SELECT count(*) FROM memory_fts').fetchone()[0] == 0
    target.close()


def test_restored_terminal_approval_is_history_and_cannot_authorize_new_memory(remembered, tmp_path):
    from test_memory_flow import knowledge_args
    source, profile, saved, *_ = remembered
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, profile['profile_id'], archive, PASSWORD)
    store = Store(tmp_path / 'target')
    restore_profile(store, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    store.close()
    target = Engine(tmp_path / 'target')
    try:
        task = target.store.get_task(saved['task_id'])
        resumed = target.call('daeri_task_resume', {'request_id': 'resume-new', 'task_id': saved['task_id'],
            'expected_task_revision': task['task_revision']})
        assert resumed['status'] == 'ok'
        args = knowledge_args(remembered)
        args['request_id'] = 'new-memory-after-restore'
        args['expected_task_revision'] = target.store.get_task(saved['task_id'])['task_revision']
        args['draft']['statement'] = 'THIS_IS_NEW_MEMORY_AFTER_RESTORE'
        count = target.store._db.execute('SELECT count(*) FROM memory_versions').fetchone()[0]
        result = target.call('daeri_memory_update', args)
        assert result['status'] == 'error', result
        assert result['error']['code'] == 'APPROVAL_UNAVAILABLE'
        assert target.store._db.execute('SELECT count(*) FROM memory_versions').fetchone()[0] == count
    finally:
        target.close()


def test_old_request_keys_are_reserved_and_safely_rejected_after_restore(tmp_path):
    source = Engine(tmp_path / 'source')
    profile = source.store.put_profile('portable')
    create = {'request_id': 'original-create', 'request_text': 'hello', 'context': {'profile_id': profile['profile_id']}}
    task = source.call('daeri_task_create', create)['data']
    pause = {'request_id': 'original-pause', 'task_id': task['task_id'], 'expected_task_revision': task['task_revision'], 'action': 'pause'}
    assert source.call('daeri_task_update', pause)['status'] == 'ok'
    # Earlier versions stored compact create caches without a profile ownership row.
    source.store._db.execute("DELETE FROM profile_references WHERE kind='request' AND reference LIKE '%original-create%'")
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    source.close()
    target_store = Store(tmp_path / 'target')
    restore_profile(target_store, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    target_store.close()
    target = Engine(tmp_path / 'target')
    try:
        for name, args in [('daeri_task_create', create), ('daeri_task_update', pause)]:
            replay = target.call(name, args)
            assert replay['status'] == 'error', replay
            assert replay['error']['code'] == 'CONTEXT_CHANGED', replay
        assert target.store._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    finally:
        target.close()


def test_restored_offset_deadlines_expire_at_their_real_utc_instant(tmp_path, monkeypatch):
    from datetime import timezone
    from daeri.portable_archive import write_bundle
    from daeri.storage import _time

    source = Engine(tmp_path / 'source')
    try:
        profile = source.store.put_profile('offset-expiry', embedding={'provider': 'keyword_only'})
        task = source.call('daeri_task_create', {'request_id': 'create', 'request_text': 'time-limited body',
            'context': {'profile_id': profile['profile_id']}})['data']
        original = tmp_path / 'original.daeri'
        backup_profile(source, profile['profile_id'], original, PASSWORD)
    finally:
        source.close()
    deadline = datetime.now(UTC) + timedelta(hours=1)
    with open_bundle(original, PASSWORD) as (manifest, archive):
        offset = deadline.astimezone(timezone(timedelta(hours=9))).isoformat()
        manifest['tasks'][0]['expires_at'] = manifest['records'][0]['expires_at'] = offset
        blobs = {}
        for name in archive.namelist():
            if name != 'manifest.json':
                copy_path = tmp_path / name.split('/')[-1]
                copy_path.write_bytes(archive.read(name))
                blobs[name] = copy_path
        altered = tmp_path / 'offset.daeri'
        write_bundle(altered, manifest, blobs, PASSWORD)
    preview = inspect_backup(altered, PASSWORD)
    target = Store(tmp_path / 'target')
    try:
        restore_profile(target, altered, PASSWORD, preview['confirmation_token'])

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return deadline + timedelta(hours=1)

        monkeypatch.setattr('daeri.storage.datetime', Later)
        with pytest.raises(StoreError, match='RECORD_EXPIRED'):
            target.read_record(task['task_id'], task['request_ref'])
        for table in ('tasks', 'records'):
            assert target._db.execute(f'SELECT expires_at FROM {table}').fetchone()[0] == _time(deadline)
    finally:
        target.close()


def test_unrelated_user_message_namespace_is_not_inferred_as_task_ownership(tmp_path):
    source = Engine(tmp_path / 'source')
    profile = source.store.put_profile('portable')
    other = source.store.put_profile('other-profile')
    task = source.call('daeri_task_create', {'request_id': 'create', 'request_text': 'hello', 'context': {'profile_id': profile['profile_id']}})['data']
    foreign = 'user_message:' + task['task_id']
    source.store.request_put(foreign, 'message', 'a' * 64, {}, profile_id=other['profile_id'])
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    with open_bundle(archive, PASSWORD) as (manifest, packed):
        assert foreign not in {row['scope'] for row in manifest['requests']}
    source.close()


def test_restoring_api_memory_builds_keyword_index_without_transmitting(remembered, tmp_path, monkeypatch):
    source, profile, *_ = remembered
    config = {key: copy.deepcopy(value) for key, value in profile.items() if key not in {'profile_id', 'revision'}}
    config['embedding'] = {'provider': 'api', 'adapter': 'openai_compatible',
        'endpoint': 'https://example.test/v1/embeddings', 'model': 'example', 'dimensions': 3,
        'credential_ref': 'credential:old-embedding'}
    source.store.put_profile(**config)
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, profile['profile_id'], archive, PASSWORD)
    def forbidden(*args, **kwargs):
        pytest.fail('Restoring an API profile must not transmit memory')
    monkeypatch.setattr('daeri.memory.create_api_embedder', forbidden)
    target = Store(tmp_path / 'target')
    result = restore_profile(target, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    assert result['index']['pending'] > 0
    assert target._db.execute('SELECT count(*) FROM memory_fts').fetchone()[0] > 0
    assert target._db.execute('SELECT count(*) FROM memory_vectors').fetchone()[0] == 0
    target.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory synchronization')
def test_directory_sync_failure_after_rename_removes_unpublished_plaintext(tmp_path, monkeypatch):
    import daeri.storage as storage
    source = Engine(tmp_path / 'source')
    source.store.put_profile('portable', core_context=['PRIVATE_CONTEXT'])
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, 'portable', archive, PASSWORD)
    source.close()
    preview = inspect_backup(archive, PASSWORD)
    target = Store(tmp_path / 'target')
    existing = target.put_profile('keep', core_context=['KEEP'])
    files_before = set((target.data_dir / 'profiles').iterdir())
    real_sync = storage.os.fsync
    calls = []
    def broken(descriptor):
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError('injected directory sync failure')
        return real_sync(descriptor)
    monkeypatch.setattr(storage.os, 'fsync', broken)
    with pytest.raises(StoreError):
        restore_profile(target, archive, PASSWORD, preview['confirmation_token'])
    assert target.get_profile('keep') == existing
    assert set((target.data_dir / 'profiles').iterdir()) == files_before
    assert list((target.data_dir / 'staging').iterdir()) == []
    target.close()


@pytest.mark.parametrize('remembered', [{'memory_retention': 'source_bound'}], indirect=True)
def test_source_bound_memory_copy_uses_shorter_current_source_deadline(remembered, tmp_path):
    source, profile, saved, *_ = remembered
    deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    source.store.set_task_expiry(saved['task_id'], deadline)
    original = source.store._db.execute('SELECT expires_at FROM memory_versions').fetchone()[0]
    archive = tmp_path / 'profile.daeri'
    backup_profile(source, profile['profile_id'], archive, PASSWORD)
    with open_bundle(archive, PASSWORD) as (manifest, packed):
        active = [row for row in manifest['memory_versions'] if row['body'] is not None]
        assert active and all(row['expires_at'] == row['body']['expires_at'] == deadline for row in active)
    assert source.store._db.execute('SELECT expires_at FROM memory_versions').fetchone()[0] == original
    target = Store(tmp_path / 'target')
    restore_profile(target, archive, PASSWORD, inspect_backup(archive, PASSWORD)['confirmation_token'])
    assert target._db.execute('SELECT expires_at FROM memory_versions').fetchone()[0] == deadline
    target.close()
