"""Task recovery keeps execution bindings without permanent copies of profile prose."""

import copy
import json
from datetime import UTC, datetime, timedelta

from daeri.engine import Engine
from daeri.storage import Store
from test_profile_delete import resource


def test_new_task_snapshot_excludes_profile_prose_and_keeps_execution_policy(tmp_path):
    engine = Engine(tmp_path)
    try:
        registered = resource('source', 'credential:unused')
        registered['capabilities'][0]['description'] = 'PRIVATE_CAPABILITY_EXPLANATION'
        profile = engine.store.put_profile('alpha', core_context=['PRIVATE_PROFILE_CONTEXT'],
                                          resources=[registered], retention_days=3, long_term_memory_days=7)
        result = engine.call('daeri_task_create', {'request_id': 'create', 'request_text': '운영 맥락 확인',
                                                 'context': {'profile_id': profile['profile_id']}})
        assert result['status'] == 'ok', result
        task = engine.store.get_task(result['data']['task_id'])
        snapshot = task['profile_snapshot']
        assert 'core_context' not in snapshot
        assert 'description' not in snapshot['resources'][0]['capabilities'][0]
        assert snapshot['retention_days'] == 3 and snapshot['long_term_memory_days'] == 7
        expected = copy.deepcopy(registered)
        expected['capabilities'][0].pop('description')
        assert snapshot['resources'] == [expected]
        assert engine.store.get_profile('alpha') == profile
        found = engine.call('daeri_memory_search', {'task_id': task['task_id'], 'query': '운영 맥락'})
        assert found['data']['core_context'] == ['PRIVATE_PROFILE_CONTEXT']
    finally:
        engine.close()


def test_reopening_scrubs_legacy_snapshot_context_after_its_profile_and_body_are_removed(tmp_path):
    engine = Engine(tmp_path)
    profile = engine.store.put_profile('alpha', core_context=['REMOVED_CONTEXT'], retention_days=1)
    created = engine.call('daeri_task_create', {'request_id': 'create', 'request_text': '임시 요청',
                                             'context': {'profile_id': profile['profile_id']}})['data']
    cancelled = engine.call('daeri_task_cancel', {'request_id': 'cancel', 'task_id': created['task_id'],
                                                 'expected_task_revision': created['task_revision']})
    assert cancelled['status'] == 'ok', cancelled
    old = engine.store.get_task(created['task_id'])
    # Simulate a file written by the prior release, including its complete snapshot.
    old['profile_snapshot'] = profile
    with engine.store.transaction():
        engine.store._db.execute('UPDATE tasks SET metadata=? WHERE task_id=?',
                                 (json.dumps(old), old['task_id']))
    engine.store.put_profile('alpha', core_context=[], retention_days=1)
    engine.close()
    reopened = Store(tmp_path)
    try:
        reopened.cleanup(now=datetime.now(UTC) + timedelta(days=3))
        row = reopened._db.execute('SELECT revision,metadata FROM tasks WHERE task_id=?', (old['task_id'],)).fetchone()
        assert 'REMOVED_CONTEXT' not in row['metadata']
        assert row['revision'] == old['task_revision']
        restored = reopened.get_task(old['task_id'])
        assert restored['state'] == 'cancelled'
        assert restored['context'] == old['context']
        assert restored['profile_snapshot']['retention_days'] == 1
        assert reopened.get_profile('alpha')['core_context'] == []
        assert not list((tmp_path / 'records').glob('*.json'))
    finally:
        reopened.close()


def test_old_in_memory_task_cannot_restore_profile_prose_on_update(tmp_path):
    store = Store(tmp_path)
    try:
        profile = store.put_profile('alpha')
        task = {'task_id': 'legacy-task', 'profile_id': profile['profile_id'], 'task_revision': 1,
                'state': 'planning', 'profile_snapshot': profile}
        store.insert_task(task)
        task['profile_snapshot']['core_context'] = ['STALE_PROCESS_CONTEXT']
        task['task_revision'] = 2
        store.update_task(task, 1)
        assert 'core_context' not in store.get_task(task['task_id'])['profile_snapshot']
        raw = store._db.execute('SELECT metadata FROM tasks WHERE task_id=?', (task['task_id'],)).fetchone()[0]
        assert 'STALE_PROCESS_CONTEXT' not in raw
    finally:
        store.close()


def test_removing_snapshot_descriptions_preserves_diagnosed_resource_execution(tmp_path):
    from test_execution_flow import approve, begin, execution, resource as cache_resource
    engine = Engine(tmp_path)
    try:
        registered = cache_resource()
        registered['capabilities'][0]['description'] = '설정에서 관리하는 조회 설명'
        profile = engine.store.put_profile('service', resources=[registered])
        engine.registry.diagnose(profile, registered['instance_id'])
        saved, draft = begin(engine, profile)
        result = engine.call('daeri_capability_execute',
                             execution(saved, approve(engine, saved), draft['steps'][0]))
        assert result['status'] == 'ok', result
        assert result['data']['inline_result']['data']['rows'][0]['amount'] == '10.00'
        snapshot = engine.store.get_task(saved['task_id'])['profile_snapshot']
        assert 'description' not in snapshot['resources'][0]['capabilities'][0]
    finally:
        engine.close()
