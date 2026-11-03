"""Oversized harness receipts retain their full evidence as immutable task files."""

from dataclasses import asdict
from hashlib import sha256
import json
import time
from uuid import UUID

import pytest

from daeri.application.agent import AgentService
from daeri.application.cloud import CloudWorkflow
from daeri.core.access import AuthenticatedUser
from daeri.core.cloud import HarnessReply
from daeri.core.errors import StoreError
from daeri.core.projects import Project
from daeri.core.serialization import validate_storable_json
from daeri.infrastructure.agentcore import AgentCoreHarness
from daeri.infrastructure.artifacts import ArtifactService
from daeri.infrastructure.cloud_s3 import S3CloudStore
from test_agentcore_harness import Client, text_events
from test_cloud_s3 import ARN, BUCKET, S3, aws_error, begin, context, manifest


MAX_BYTES = 8 * 1024 * 1024


class Files(S3):
    def list_objects_v2(self, **request):
        if 'Delimiter' in request:
            return super().list_objects_v2(**request)
        return {'Contents': [{'Key': key, 'Size': len(body)}
                             for key, (body, _) in self.objects.items()
                             if key.startswith(request['Prefix'])]}

    def head_object(self, **request):
        key = request['Key']
        version = request.get('VersionId', self.current_versions[key])
        return {'VersionId': version, 'ContentLength': len(self.versions[key, version][0])}

    def generate_presigned_url(self, operation, **request):
        self.calls.append(('sign', request))
        return 'https://downloads.example/original'


class Catalog:
    def get(self, project_id):
        return Project(project_id, 'Project', ARN, 'RELEASE_V2', '2')


def test_large_native_reply_returns_the_saved_error_and_an_owner_scoped_download():
    client, s3 = Client(text_events('x' * MAX_BYTES)), Files()
    store = S3CloudStore(s3, BUCKET)
    workflow = CloudWorkflow(store, AgentCoreHarness(client), Catalog())
    service = AgentService(workflow, check_harness=lambda route: None,
                           artifacts=ArtifactService(s3, BUCKET))
    user = AuthenticatedUser('alice', frozenset({'project'}), int(time.time()) + 3600)

    result = service.work(user, 'project', 'Report the result')

    assert result['error'] == 'HARNESS_INVALID_OUTPUT' and result['state'] == 'interrupted'
    assert not result['awaiting_confirmation'] and len(client.requests) == 1
    task = workflow.get_task(user.context('project'), result['task_id'])
    saved = store.read_turn(task.context, task.id)
    assert saved.reply.text == result['response']
    assert store.recover_turn(task.context, task.id).reply == saved.reply
    listed = service.files(user, 'project', task.id)
    assert len(listed['items']) == 1 and listed['items'][0]['size'] > MAX_BYTES
    item = listed['items'][0]
    download = service.download(user, 'project', task.id, item['id'])
    assert download['name'] == UUID(saved.turn_id).hex + '.json'
    signed = next(call[1] for call in s3.calls if call[0] == 'sign')
    raw_key = signed['Params']['Key']
    assert signed['Params']['VersionId'] == s3.current_versions[raw_key]
    assert json.loads(s3.objects[raw_key][0])['reply']['text'] == 'x' * MAX_BYTES
    bob = AuthenticatedUser('bob', user.project_ids, user.expires_at)
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        service.download(bob, 'project', task.id, item['id'])
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store._get(raw_key)


@pytest.mark.parametrize('location', ['pending_tools', 'observations'])
def test_large_handoff_or_observation_is_preserved_but_cannot_remain_actionable(location):
    store, task, turn = begin(S3())
    pending = ({'toolUseId': 'plan', 'name': 'confirm_plan', 'input': {'plan': 'x' * MAX_BYTES}},)
    observations = ({'toolUseId': 'query', 'content': [{'text': 'x' * MAX_BYTES}]},)
    original = HarnessReply('Partial result', 'tool_use',
                            pending if location == 'pending_tools' else (),
                            observations if location == 'observations' else (), 'HARNESS_RUNTIME_ERROR')

    stored = store.finish_turn(task, turn, 'Inspect', original, ())

    assert stored.error == 'HARNESS_INVALID_OUTPUT' and not stored.pending_tools
    current = store.get_task(task.context, task.id)
    assert current.state == 'interrupted' and current.approval is None
    receipt, _ = store._get(manifest(store.client)[1]['last_turn'])
    full = store.read_original_receipt(receipt)
    assert full['reply'] == json.loads(validate_storable_json(asdict(original)))


def test_combined_request_and_reply_overflow_keeps_exact_input_with_a_maximum_prefix():
    s3 = S3()
    store = S3CloudStore(s3, BUCKET, 'x' * 787)
    task = store.create_task(context(), ARN, 'g', harness_endpoint='RELEASE_V2', harness_version='2')
    _, before = manifest(s3)
    candidate = {**before, 'in_flight': {'turn_id': '0' * 8 + '-0000-0000-0000-' + '0' * 12,
                                       'prompt': '', 'tool_results': []}}
    prompt = 'p' * (MAX_BYTES - len(validate_storable_json(candidate)))
    turn = store.begin_turn(task, prompt, ())

    stored = store.finish_turn(task, turn, prompt, HarnessReply('r' * 4096, 'end_turn'), ())

    assert stored.error == 'HARNESS_INVALID_OUTPUT'
    key, state = manifest(s3)
    assert len(s3.objects[key][0]) < MAX_BYTES and state['in_flight'] is None
    receipt, _ = store._get(state['last_turn'])
    original = store.read_original_receipt(receipt)
    assert original['prompt'] == prompt and original['tool_results'] == []
    assert max(len(key.encode()) for key in s3.objects) == 1024


@pytest.mark.parametrize('failure', ['raw_before', 'raw_after', 'receipt_before', 'receipt_after'])
def test_offload_write_failures_keep_actual_execution_recoverable_when_the_original_exists(failure):
    class Interrupted(S3):
        def __init__(self):
            super().__init__()
            self.armed = False

        def put_object(self, **request):
            selected = '/artifacts/' in request['Key'] if failure.startswith('raw_') else '/turns/' in request['Key']
            if self.armed and selected:
                self.armed = False
                if failure.endswith('before'):
                    raise aws_error('InternalError', 500)
                super().put_object(**request)
                raise TimeoutError('PRIVATE acknowledgement lost')
            return super().put_object(**request)

    s3 = Interrupted()
    store, task, turn = begin(s3)
    s3.armed = True
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store.finish_turn(task, turn, 'Inspect', HarnessReply('x' * MAX_BYTES, 'end_turn'), ())
    if failure != 'receipt_after':
        with pytest.raises(StoreError, match='EXECUTION_UNCERTAIN'):
            store.recover_turn(task.context, task.id)
        assert store.get_task(task.context, task.id).state == 'running'
    else:
        recovered = store.recover_turn(task.context, task.id)
        assert recovered.reply.error == 'HARNESS_INVALID_OUTPUT'
        assert store.get_task(task.context, task.id).state == 'interrupted'


def test_repeated_large_receipt_is_idempotent_and_different_originals_never_overwrite():
    s3 = S3()
    store, task, turn = begin(s3)
    original = HarnessReply('x' * MAX_BYTES, 'end_turn')
    stored = store.finish_turn(task, turn, 'Inspect', original, ())
    snapshot = dict(s3.objects)
    assert store.finish_turn(task, turn, 'Inspect', original, ()) == stored
    assert s3.objects == snapshot
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        store.finish_turn(task, turn, 'Inspect', HarnessReply('y' * MAX_BYTES, 'end_turn'), ())
    assert s3.objects == snapshot


def test_a_later_artifact_upload_cannot_replace_the_preserved_original_or_its_download():
    s3 = Files()
    store, task, turn = begin(s3)
    original = HarnessReply('x' * MAX_BYTES, 'end_turn')
    store.finish_turn(task, turn, 'Inspect', original, ())
    receipt, _ = store._get(manifest(s3)[1]['last_turn'])
    reference = receipt['original_receipt']
    artifacts = ArtifactService(s3, BUCKET)
    user = AuthenticatedUser('alice', frozenset({'project'}), int(time.time()) + 3600)
    item = artifacts.list(user, task)['items'][0]
    s3.put_object(Bucket=BUCKET, Key=reference['key'], Body=b'{"later":"upload"}')

    preserved = store.read_original_receipt(receipt)

    assert preserved['reply']['text'] == original.text
    download = artifacts.download(user, task, item['id'])
    signed = next(call[1] for call in s3.calls if call[0] == 'sign')
    assert signed['Params']['VersionId'] == reference['version_id']
    assert download['size'] == reference['bytes']
    assert artifacts.list(user, task)['items'][0]['size'] == reference['bytes']
    assert store.finish_turn(task, turn, 'Inspect', original, ()).error == 'HARNESS_INVALID_OUTPUT'


def test_an_original_artifact_cannot_use_another_turns_receipt_in_the_same_task():
    s3 = Files()
    store, task, first_turn = begin(s3)
    store.finish_turn(task, first_turn, 'Inspect', HarnessReply('x' * MAX_BYTES, 'end_turn'), ())
    first_receipt_key = manifest(s3)[1]['last_turn']
    current = store.get_task(task.context, task.id)
    second_turn = store.begin_turn(current, 'Next', ())
    store.finish_turn(current, second_turn, 'Next', HarnessReply('y' * (MAX_BYTES + 10), 'end_turn'), ())
    second_receipt_key = manifest(s3)[1]['last_turn']
    s3.objects[first_receipt_key] = s3.objects[second_receipt_key]

    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store.original_artifact(current, UUID(first_turn).hex + '.json')


@pytest.mark.parametrize('field,value', [
    ('key', 'daeri/foreign.json'), ('bytes', True), ('sha256', '0' * 64),
    ('input_sha256', '0' * 64), ('version_id', 'null'), ('version_id', None), ('version_id', 'foreign'),
])
def test_full_original_reads_reject_changed_artifact_references_or_input_fingerprints(field, value):
    store, task, turn = begin(S3())
    store.finish_turn(task, turn, 'Inspect', HarnessReply('x' * MAX_BYTES, 'end_turn'), ())
    receipt, _ = store._get(manifest(store.client)[1]['last_turn'])
    receipt['original_receipt'][field] = value
    with pytest.raises(StoreError, match='STORAGE_(CONFLICT|UNAVAILABLE)'):
        store.read_original_receipt(receipt)


@pytest.mark.parametrize('change', ['missing', 'bytes', 'actor_id', 'session_id', 'prompt', 'tool_results', 'error'])
def test_full_original_reads_reject_missing_changed_or_rebound_artifacts(change):
    s3 = S3()
    store, task, turn = begin(s3)
    store.finish_turn(task, turn, 'Inspect', HarnessReply('x' * MAX_BYTES, 'end_turn'), ())
    receipt, _ = store._get(manifest(s3)[1]['last_turn'])
    reference = receipt['original_receipt']
    key = reference['key']
    if change == 'missing':
        del s3.objects[key]
        del s3.versions[key, reference['version_id']]
    elif change == 'bytes':
        data, etag = s3.objects[key]
        s3.versions[key, reference['version_id']] = (data.replace(b'xxxxxxxx', b'yyyyyyyy', 1), etag)
    else:
        original = json.loads(s3.objects[key][0])
        if change == 'error':
            original['reply']['error'] = 'HARNESS_UNAVAILABLE'
        elif change == 'tool_results':
            original[change] = [{'toolUseId': 'foreign', 'content': [{'text': 'Different input'}]}]
        elif change == 'session_id':
            original[change] = '00000000-0000-0000-0000-000000000000'
        else:
            original[change] = 'foreign'
        data = validate_storable_json(original)
        s3.versions[key, reference['version_id']] = (data, 'changed')
        reference.update(bytes=len(data), sha256=sha256(data).hexdigest())
    with pytest.raises(StoreError, match='STORAGE_(CONFLICT|UNAVAILABLE)'):
        store.read_original_receipt(receipt)


def test_recovery_rejects_a_compact_receipt_with_different_pending_inputs():
    s3 = S3()
    store, task, turn = begin(s3)
    s3.fail_manifest = 'before'
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store.finish_turn(task, turn, 'Inspect', HarnessReply('x' * MAX_BYTES, 'end_turn'), ())
    key = next(key for key in s3.objects if '/turns/' in key)
    receipt = json.loads(s3.objects[key][0])
    receipt['original_receipt']['input_sha256'] = '0' * 64
    s3.objects[key] = (validate_storable_json(receipt), 'changed')
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        store.recover_turn(task.context, task.id)
    assert store.get_task(task.context, task.id).state == 'running'


def test_an_ordinary_artifact_with_a_uuid_filename_keeps_current_version_downloads():
    s3 = Files()
    store = S3CloudStore(s3, BUCKET)
    task = store.create_task(context(), ARN, 'Inspect')
    artifacts = ArtifactService(s3, BUCKET)
    user = AuthenticatedUser('alice', frozenset({'project'}), int(time.time()) + 3600)
    key = artifacts.location(task)['prefix'] + '0' * 32 + '.json'
    s3.put_object(Bucket=BUCKET, Key=key, Body=b'first')
    item = artifacts.list(user, task)['items'][0]
    updated = s3.put_object(Bucket=BUCKET, Key=key, Body=b'updated file')

    result = artifacts.download(user, task, item['id'])

    signed = next(call[1] for call in s3.calls if call[0] == 'sign')
    assert signed['Params']['VersionId'] == updated['VersionId']
    assert result['size'] == len(b'updated file')


@pytest.mark.parametrize('version', [None, 'null'])
def test_offload_without_a_persistent_s3_version_fails_before_recording_a_recoverable_receipt(version):
    class Unversioned(S3):
        def put_object(self, **request):
            response = super().put_object(**request)
            if '/artifacts/' in request['Key']:
                response['VersionId'] = version
            return response

    s3 = Unversioned()
    store, task, turn = begin(s3)
    with pytest.raises(StoreError, match='ARTIFACT_UNAVAILABLE'):
        store.finish_turn(task, turn, 'Inspect', HarnessReply('x' * MAX_BYTES, 'end_turn'), ())
    assert not any('/turns/' in key for key in s3.objects)
    with pytest.raises(StoreError, match='EXECUTION_UNCERTAIN'):
        store.recover_turn(task.context, task.id)
