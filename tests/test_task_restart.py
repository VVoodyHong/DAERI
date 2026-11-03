"""A reviewed recovery rotates sessions without replaying uncertain operations."""

from dataclasses import replace
import asyncio
import json
from uuid import uuid4

import pytest

from daeri.core.cloud import AccessContext, HarnessReply
from daeri.core.errors import StoreError
from daeri.infrastructure.cloud_s3 import S3CloudStore
from test_cloud_s3 import S3, ARN, BUCKET, manifest
from test_agent_service import setup, answer, PENDING


def test_restart_archives_the_original_execution_and_revokes_the_old_session():
    store = S3CloudStore(S3(), BUCKET)
    context = AccessContext('alice', 'project')
    task = store.create_task(context, ARN, 'Compare resources', harness_endpoint='RELEASE_V2', harness_version='2')
    turn_id = store.begin_turn(task, 'Compare resources', ())
    current = store.get_task(context, task.id)
    restored = store.restart_task(current, 'Previous execution stopped; the report was not delivered.')
    assert restored.id == task.id and restored.session_id != task.session_id
    assert restored.state == 'active' and not restored.pending_tools and restored.approval is None
    assert restored.harness_endpoint == 'RELEASE_V2' and store.read_turn(context, task.id) is None
    archives = [json.loads(body) for key, (body, _) in store.client.objects.items() if '/reset/' in key]
    assert len(archives) == 1 and archives[0]['previous']['in_flight']['turn_id'] == turn_id
    assert archives[0]['previous']['session_id'] == task.session_id
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        store.hook_get(ARN, task.session_id)


def test_restart_rejects_a_changed_task_before_writing_any_recovery_record():
    store = S3CloudStore(S3(), BUCKET)
    task = store.create_task(AccessContext('alice', 'project'), ARN, 'Read data')
    snapshot = dict(store.client.objects)
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        store.restart_task(replace(task, etag='stale'), 'Reviewed the previous effects.')
    assert store.client.objects == snapshot


def test_reviewed_restart_creates_a_new_plan_and_requires_a_new_approval():
    service, user, store, harness = setup(HarnessReply('Old session result', 'end_turn'),
        HarnessReply('Review new plan', 'tool_use', (PENDING,)), HarnessReply('Recovered', 'end_turn'))
    first = service.work(user, 'project', 'Compare resources')
    original = store.get_task(user.context('project'), first['task_id'])
    async def review(goal, state):
        return 'The old session ended. The last report is valid; only the new comparison remains.'
    renewed = asyncio.run(service.restart(user, 'project', original.id, review))
    assert renewed['awaiting_confirmation'] and renewed['approval'] == 'pending'
    assert store.get_task(user.context('project'), original.id).session_id != original.session_id
    assert harness.guards[1]['decision'] == 'deny'
    request = harness.calls[1][1][0]['content'][0]['text']
    assert 'Old session result' in request and 'Compare resources' in request
    result = asyncio.run(service.confirm(user, 'project', original.id, answer))
    assert result['response'] == 'Recovered' and harness.guards[2]['decision'] == 'allow'


def test_cancelled_recovery_or_changed_task_cannot_rotate_or_execute():
    service, user, store, harness = setup(HarnessReply('Original', 'end_turn'))
    first = service.work(user, 'project', 'Compare resources')
    original = store.get_task(user.context('project'), first['task_id'])
    async def cancel(goal, state):
        return None
    asyncio.run(service.restart(user, 'project', original.id, cancel))
    assert store.get_task(user.context('project'), original.id).session_id == original.session_id
    async def change(goal, state):
        store.begin_turn(store.get_task(user.context('project'), original.id), 'Changed meanwhile', ())
        return 'Reviewed previous effects.'
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        asyncio.run(service.restart(user, 'project', original.id, change))
    assert store.get_task(user.context('project'), original.id).session_id == original.session_id
    assert len(harness.calls) == 1


def test_failed_new_session_start_keeps_the_archived_result_visible_and_available_for_retry(monkeypatch):
    service, user, store, harness = setup(HarnessReply('Verified original result', 'end_turn'),
                                        HarnessReply('Review new plan', 'tool_use', (PENDING,)))
    first = service.work(user, 'project', 'Compare resources')
    original_begin = store.begin_turn
    def unavailable(*args, **kwargs):
        raise StoreError('STORAGE_UNAVAILABLE', 'The start write was not confirmed.')
    monkeypatch.setattr(store, 'begin_turn', unavailable)
    async def reviewed(goal, state):
        return 'The old execution ended; continue from the verified result.'
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        asyncio.run(service.restart(user, 'project', first['task_id'], reviewed))
    snapshot = dict(store.client.objects)
    assert service.status(user, 'project', first['task_id'])['response'] == 'Verified original result'
    assert store.client.objects == snapshot
    with pytest.raises(StoreError, match='EXECUTION_UNCERTAIN'):
        store.recover_turn(user.context('project'), first['task_id'])
    monkeypatch.setattr(store, 'begin_turn', original_begin)
    result = asyncio.run(service.restart(user, 'project', first['task_id'], reviewed))
    assert result['awaiting_confirmation'] and len(harness.calls) == 2
    assert 'Verified original result' in harness.calls[1][1][0]['content'][0]['text']


@pytest.mark.parametrize('corruption', [
    'project_id', 'actor_id', 'id', 'harness_arn', 'harness_endpoint', 'harness_version',
    'new_session_id', 'last_turn', 'cycle', 'review', 'reset_key', 'missing_receipt', 'receipt_binding',
])
def test_archived_result_fallback_rejects_foreign_corrupt_or_cyclic_reset_state(corruption):
    service, user, store, _ = setup(HarnessReply('Original result', 'end_turn'))
    first = service.work(user, 'project', 'Compare resources')
    task = store.get_task(user.context('project'), first['task_id'])
    restored = store.restart_task(task, 'The original run ended.')
    manifest_key, current = manifest(store.client)
    reset_key = current['recovery']
    reset = json.loads(store.client.objects[reset_key][0])
    if corruption in {'project_id', 'actor_id', 'harness_arn', 'harness_endpoint', 'harness_version'}:
        reset['previous'][corruption] = 'foreign'
    elif corruption == 'id':
        reset['previous']['id'] = str(uuid4())
    elif corruption == 'new_session_id':
        reset['new_session_id'] = str(uuid4())
    elif corruption == 'last_turn':
        reset['previous']['last_turn'] = 'daeri/foreign/turns/' + str(uuid4()) + '.json'
    elif corruption == 'cycle':
        reset['previous']['session_id'] = restored.session_id
        reset['previous']['last_turn'] = None
        reset['previous']['recovery'] = reset_key
    elif corruption == 'review':
        reset['review'] = None
    elif corruption == 'missing_receipt':
        del store.client.objects[reset['previous']['last_turn']]
    elif corruption == 'receipt_binding':
        receipt_key = reset['previous']['last_turn']
        receipt = json.loads(store.client.objects[receipt_key][0])
        receipt['session_id'] = str(uuid4())
        store.client.objects[receipt_key] = (json.dumps(receipt).encode(), 'changed')
    else:
        forged_key = reset_key.rsplit('/', 1)[0] + '/' + str(uuid4()) + '.json'
        current['recovery'] = forged_key
        store.client.objects[manifest_key] = (json.dumps(current).encode(), 'changed')
        reset_key = forged_key
    store.client.objects[reset_key] = (json.dumps(reset).encode(), 'changed')
    snapshot = dict(store.client.objects)
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store.read_turn(user.context('project'), first['task_id'])
    assert store.client.objects == snapshot
