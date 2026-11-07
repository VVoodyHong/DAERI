"""Offline S3 CAS and immutable receipt contracts; no AWS network requests."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from threading import Barrier, Lock

import boto3
from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.stub import ANY, Stubber
from botocore.validate import validate_parameters
import pytest

from daeri.core.cloud import AccessContext, HarnessReply
from daeri.core.errors import StoreError


BUCKET = 'company-daeri'
ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/project-1234567890'
MODEL = get_session().get_service_model('s3')


def aws_error(code, status):
    return ClientError({'Error': {'Code': code, 'Message': 'password=PRIVATE'},
                        'ResponseMetadata': {'HTTPStatusCode': status}}, 'PutObject')


class S3:
    """Atomic object-map double; requests still use the installed SDK schemas."""

    def __init__(self):
        self.objects, self.calls, self.streams = {}, [], []
        self.versions, self.current_versions = {}, {}
        self.lock = Lock()
        self.fail_manifest = None

    def put_object(self, **request):
        validate_parameters(request, MODEL.operation_model('PutObject').input_shape)
        with self.lock:
            self.calls.append(('put', request))
            key, old = request['Key'], self.objects.get(request['Key'])
            failure = self.fail_manifest if key.endswith('/manifest.json') else None
            if failure == 'before':
                self.fail_manifest = None
                raise aws_error('InternalError', 500)
            if failure == 'conflict':
                self.fail_manifest = None
                raise aws_error('PreconditionFailed', 412)
            if request.get('IfNoneMatch') == '*' and old is not None:
                raise aws_error('PreconditionFailed', 412)
            if 'IfMatch' in request and (old is None or old[1] != request['IfMatch']):
                raise aws_error('PreconditionFailed', 412)
            body = request['Body']
            etag = '"' + sha256(body).hexdigest() + '"'
            self.objects[key] = (body, etag)
            version = str(len(self.versions) + 1)
            self.versions[key, version] = (body, etag)
            self.current_versions[key] = version
            if failure == 'after':
                self.fail_manifest = None
                raise TimeoutError('password=PRIVATE')
            return {'ETag': etag, 'VersionId': version}

    def get_object(self, **request):
        validate_parameters(request, MODEL.operation_model('GetObject').input_shape)
        with self.lock:
            self.calls.append(('get', request))
            key, version = request['Key'], request.get('VersionId')
            if key not in self.objects or version is not None and (key, version) not in self.versions:
                raise aws_error('NoSuchKey', 404)
            body, etag = self.versions[key, version] if version is not None else self.objects[key]
            stream = BytesIO(body)
            self.streams.append(stream)
            return {'Body': stream, 'ETag': etag, 'ContentLength': len(body),
                    'VersionId': version or self.current_versions.get(key)}

    def list_objects_v2(self, **request):
        validate_parameters(request, MODEL.operation_model('ListObjectsV2').input_shape)
        self.calls.append(('list', request))
        prefix = request['Prefix']
        groups = sorted({prefix + key[len(prefix):].split('/')[0] + '/'
                         for key in self.objects if key.startswith(prefix)})
        offset = int(request.get('ContinuationToken', '0'))
        end = offset + request['MaxKeys']
        response = {'CommonPrefixes': [{'Prefix': group} for group in groups[offset:end]],
                    'IsTruncated': end < len(groups)}
        if response['IsTruncated']:
            response['NextContinuationToken'] = str(end)
        return response


def make_store(client):
    from daeri.infrastructure.cloud_s3 import S3CloudStore
    return S3CloudStore(client, BUCKET)


def context():
    return AccessContext('alice', 'project')


def begin(client):
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect', harness_endpoint='RELEASE_V1', harness_version='1')
    turn = store.begin_turn(task, 'Inspect', ())
    return store, task, turn


def manifest(client):
    key = next(key for key in client.objects if key.endswith('/manifest.json'))
    return key, json.loads(client.objects[key][0])


def test_manifest_creation_and_loading_preserve_binding_route_and_goal():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect\x00the source')
    loaded = make_store(client).get_task(context(), task.id)
    assert loaded == task and loaded.harness_arn == ARN and loaded.goal == 'Inspect\x00the source'
    assert task.etag and task.session_id and task.id
    assert client.calls[0][1]['IfNoneMatch'] == '*'
    assert all(stream.closed for stream in client.streams)


def test_release_route_and_readonly_last_result_survive_a_new_execution():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect', harness_endpoint='RELEASE_V2', harness_version='2')
    assert store.read_turn(context(), task.id) is None
    turn = store.begin_turn(task, 'Inspect', ())
    store.finish_turn(task, turn, 'Inspect', HarnessReply('Original result', 'end_turn'), ())
    loaded = make_store(client).get_task(context(), task.id)
    assert loaded.harness_endpoint == 'RELEASE_V2' and loaded.harness_version == '2'
    store.begin_turn(loaded, 'Follow up', ())
    calls = len(client.calls)
    result = make_store(client).read_turn(context(), task.id)
    assert result.reply.text == 'Original result'
    assert all(operation != 'put' for operation, _ in client.calls[calls:])


def test_modified_release_route_in_receipt_is_rejected():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect', harness_endpoint='RELEASE_V2', harness_version='2')
    turn = store.begin_turn(task, 'Inspect', ())
    store.finish_turn(task, turn, 'Inspect', HarnessReply('Original result', 'end_turn'), ())
    key = next(key for key in client.objects if '/turns/' in key)
    receipt = json.loads(client.objects[key][0])
    receipt['harness_version'] = '3'
    client.objects[key] = (json.dumps(receipt).encode(), 'changed')
    with pytest.raises(StoreError) as error:
        store.read_turn(context(), task.id)
    assert error.value.code == 'STORAGE_CONFLICT'


def test_two_concurrent_stale_writers_only_allow_one_external_invocation():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect')
    invocations = []
    def invoke(_):
        try:
            turn_id = store.begin_turn(task, 'Inspect', ())
            invocations.append(turn_id)
            return 'invoked'
        except StoreError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(invoke, range(2)))
    assert sorted(outcomes) == ['TASK_BUSY', 'invoked'] and len(invocations) == 1
    assert any(request.get('IfMatch') == task.etag for operation, request in client.calls if operation == 'put')


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_receipt_recovers_commit_failure_or_ack_loss_without_reinvoking(failure):
    client = S3()
    store, task, turn = begin(client)
    reply = HarnessReply('Done', 'end_turn')
    client.fail_manifest = failure
    with pytest.raises(StoreError) as error:
        store.finish_turn(task, turn, 'Inspect', reply, ())
    assert 'PRIVATE' not in str(error.value)
    _, state = manifest(client)
    assert bool(state['in_flight']) == (failure == 'before')
    recovered = make_store(client).recover_turn(context(), task.id)
    assert recovered.task_id == task.id and recovered.turn_id == turn and recovered.reply == reply
    assert manifest(client)[1]['in_flight'] is None
    again = make_store(client).recover_turn(context(), task.id)
    assert again == recovered


def test_missing_receipt_keeps_execution_uncertain_and_blocks_new_turn():
    client = S3()
    store, task, turn = begin(client)
    with pytest.raises(StoreError, match='EXECUTION_UNCERTAIN'):
        store.recover_turn(context(), task.id)
    assert manifest(client)[1]['in_flight']['turn_id'] == turn
    with pytest.raises(StoreError, match='TASK_BUSY'):
        store.begin_turn(store.get_task(context(), task.id), 'Retry', ())


def test_recovery_cas_conflict_keeps_the_recorded_execution_pending():
    client = S3()
    store, task, turn = begin(client)
    client.fail_manifest = 'before'
    with pytest.raises(StoreError):
        store.finish_turn(task, turn, 'Inspect', HarnessReply('Done', 'end_turn'), ())
    client.fail_manifest = 'conflict'
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        store.recover_turn(context(), task.id)
    assert manifest(client)[1]['in_flight']['turn_id'] == turn
    assert store.recover_turn(context(), task.id).turn_id == turn


def test_receipt_is_immutable_and_keeps_all_actual_tool_results():
    client = S3()
    store, task, turn = begin(client)
    pending = ({'toolUseId': 'approve', 'name': 'approve', 'input': {'plan': 'Inspect'}},)
    store.finish_turn(task, turn, 'Inspect', HarnessReply('Approve', 'tool_use', pending), ())
    task = store.get_task(context(), task.id)
    results = ({'toolUseId': 'approve', 'status': 'success', 'content': [{'text': 'Approved\x00by user'}]},)
    turn = store.begin_turn(task, '', results)
    reply = HarnessReply('Done', 'end_turn')
    store.finish_turn(task, turn, '', reply, results)
    receipt_key = manifest(client)[1]['last_turn']
    receipt_before = client.objects[receipt_key]
    assert json.loads(receipt_before[0])['tool_results'] == list(results)
    store.finish_turn(task, turn, '', reply, results)
    assert client.objects[receipt_key] == receipt_before
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        store.finish_turn(task, turn, '', HarnessReply('Different', 'end_turn'), results)
    assert client.objects[receipt_key] == receipt_before


@pytest.mark.parametrize('reply', [
    HarnessReply('Partial', 'error', error='HARNESS_UNAVAILABLE'),
    HarnessReply('Invalid response', 'invalid_output', error='HARNESS_INVALID_OUTPUT'),
])
def test_interrupted_receipt_is_preserved_and_explicit_follow_up_can_start(reply):
    client = S3()
    store, task, turn = begin(client)
    store.finish_turn(task, turn, 'Inspect', reply, ())
    assert manifest(client)[1]['state'] == 'interrupted'
    assert store.recover_turn(context(), task.id).reply == reply
    next_turn = store.begin_turn(store.get_task(context(), task.id), 'I checked the execution; continue', ())
    assert next_turn != turn


def test_throttled_approval_can_be_explicitly_resubmitted_through_the_real_workflow():
    from daeri.application.cloud import CloudWorkflow
    from daeri.infrastructure.agentcore import AgentCoreHarness
    from daeri.core.projects import Project

    class Catalog:
        def get(self, project_id):
            return Project(project_id, 'Project', ARN)

    class HarnessClient:
        def __init__(self):
            self.requests = []

        def invoke_harness(self, **request):
            validate_parameters(request, get_session().get_service_model('bedrock-agentcore')
                                .operation_model('InvokeHarness').input_shape)
            self.requests.append(request)
            if len(self.requests) == 1:
                raise ClientError({'Error': {'Code': 'ThrottlingException', 'Message': 'PRIVATE'}}, 'InvokeHarness')
            return {'stream': (event for event in [
                {'messageStart': {'role': 'assistant'}},
                {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'text': 'Done'}}},
                {'contentBlockStop': {'contentBlockIndex': 0}},
                {'messageStop': {'stopReason': 'end_turn'}},
            ])}

    client = S3()
    store, task, turn = begin(client)
    pending = ({'toolUseId': 'approve', 'name': 'confirm_plan', 'input': {'plan': 'Inspect'}},)
    store.finish_turn(task, turn, 'Inspect', HarnessReply('Approve', 'tool_use', pending), ())
    harness_client = HarnessClient()
    workflow = CloudWorkflow(store, AgentCoreHarness(harness_client), Catalog())
    actual = ({'toolUseId': 'approve', 'status': 'success', 'content': [{'text': 'Approved by user'}]},)

    first = workflow.resume(context(), task.id, tool_results=actual)
    assert first.reply.error == 'HARNESS_THROTTLED'
    assert store.get_task(context(), task.id).pending_tools == pending
    second = workflow.resume(context(), task.id, tool_results=actual)
    assert second.reply.text == 'Done' and second.reply.error is None
    assert store.get_task(context(), task.id).pending_tools == ()
    assert len(harness_client.requests) == 2
    assert all(request['messages'][0]['content'][0]['toolResult']['toolUseId'] == 'approve'
               for request in harness_client.requests)


@pytest.mark.parametrize('other', [('bob', 'project'), ('alice', 'another')])
def test_task_cannot_be_read_through_another_actor_or_project(other):
    client = S3()
    store, task, _ = begin(client)
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        store.get_task(AccessContext(*other), task.id)
    other_key = client.calls[-1][1]['Key']
    original_key, _ = manifest(client)
    assert other_key != original_key
    client.objects[other_key] = client.objects[original_key]
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        store.get_task(AccessContext(*other), task.id)


def test_state_keys_hash_identity_and_project_instead_of_accepting_path_segments():
    client = S3()
    unusual = AccessContext('../alice/private', '../../project')
    task = make_store(client).create_task(unusual, ARN, 'Inspect')
    key, _ = manifest(client)
    assert '../' not in key and 'alice' not in key and len(key.encode()) <= 1024
    assert make_store(client).get_task(unusual, task.id).context == unusual


@pytest.mark.parametrize('task_id', ['', '../manifest', 'a/b', 'not-a-uuid', None])
def test_invalid_task_identifier_is_rejected_before_s3(task_id):
    client = S3()
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        make_store(client).get_task(context(), task_id)
    assert client.calls == []


def test_recovery_rejects_wrong_or_malformed_receipt_without_unlocking():
    client = S3()
    store, task, turn = begin(client)
    client.fail_manifest = 'before'
    with pytest.raises(StoreError):
        store.finish_turn(task, turn, 'Inspect', HarnessReply('Done', 'end_turn'), ())
    receipt_key = next(key for key in client.objects if '/turns/' in key)
    payload, etag = client.objects[receipt_key]
    wrong = json.loads(payload)
    wrong['actor_id'] = 'bob'
    for body in [json.dumps(wrong).encode(), b'{bad-json', b'[]']:
        client.objects[receipt_key] = (body, etag)
        with pytest.raises(StoreError) as error:
            store.recover_turn(context(), task.id)
        assert error.value.code in {'STORAGE_CONFLICT', 'STORAGE_UNAVAILABLE'}
        assert manifest(client)[1]['in_flight']['turn_id'] == turn


def test_oversized_manifest_is_rejected_and_the_body_is_closed():
    client = S3()
    store, task, _ = begin(client)
    key, _ = manifest(client)
    client.objects[key] = (b' ' * (8 * 1024 * 1024 + 1), '"oversized"')
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store.get_task(context(), task.id)
    assert client.streams[-1].closed


def test_saved_manifest_must_be_utf8_json_with_unique_fields():
    client = S3()
    store, task, _ = begin(client)
    key, original = manifest(client)
    for body in [json.dumps(original).encode('utf-16'), b'{"id":1,"id":2}']:
        client.objects[key] = (body, '"bad-encoding"')
        with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
            store.get_task(context(), task.id)
        assert client.streams[-1].closed


def test_begin_checks_pending_call_snapshot_and_actual_response_ids_before_writing():
    client = S3()
    store, task, turn = begin(client)
    pending = ({'toolUseId': 'approve', 'name': 'approve', 'input': {}},)
    store.finish_turn(task, turn, 'Inspect', HarnessReply('Approve', 'tool_use', pending), ())
    task = store.get_task(context(), task.id)
    writes = len([1 for name, _ in client.calls if name == 'put'])
    with pytest.raises(StoreError, match='TASK_BUSY'):
        store.begin_turn(replace(task, pending_tools=()), 'Continue', ())
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        store.begin_turn(task, 'Continue', ())
    assert len([1 for name, _ in client.calls if name == 'put']) == writes


def test_creation_ack_loss_exposes_the_created_task_identifier_for_inspection():
    client = S3()
    client.fail_manifest = 'after'
    with pytest.raises(StoreError) as caught:
        make_store(client).create_task(context(), ARN, 'Inspect')
    assert caught.value.task_id
    assert make_store(client).get_task(context(), caught.value.task_id).goal == 'Inspect'


def test_begin_ack_loss_exposes_the_pending_turn_without_claiming_execution_happened():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect')
    client.fail_manifest = 'after'
    with pytest.raises(StoreError) as caught:
        store.begin_turn(task, 'Inspect', ())
    assert caught.value.task_id == task.id
    assert caught.value.turn_id == manifest(client)[1]['in_flight']['turn_id']
    with pytest.raises(StoreError, match='EXECUTION_UNCERTAIN'):
        store.recover_turn(context(), task.id)


@pytest.mark.parametrize('response', [None, {'Error': []}, {'Error': {'Code': {}}}])
@pytest.mark.parametrize('operation', ['get', 'put'])
def test_malformed_sdk_errors_never_leak_raw_exception_or_metadata(response, operation):
    failure = RuntimeError('password=PRIVATE')
    failure.response = response
    class BrokenS3:
        def get_object(self, **request):
            raise failure
        def put_object(self, **request):
            raise failure
    with pytest.raises(StoreError) as caught:
        store = make_store(BrokenS3())
        if operation == 'get':
            store.get_task(context(), '01234567-89ab-cdef-0123-456789abcdef')
        else:
            store.create_task(context(), ARN, 'Inspect')
    assert caught.value.code == 'STORAGE_UNAVAILABLE' and 'PRIVATE' not in str(caught.value)


def test_normalized_prefix_leaves_room_for_hashed_scopes_and_both_uuids():
    from daeri.infrastructure.cloud_s3 import S3CloudStore, normalize_storage_prefix
    assert len(normalize_storage_prefix('x' * 787)) == 788
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        normalize_storage_prefix('x' * 788)
    client = S3()
    store = S3CloudStore(client, BUCKET, 'x' * 787)
    task = store.create_task(context(), ARN, 'Inspect')
    turn = store.begin_turn(task, 'Inspect', ())
    store.finish_turn(task, turn, 'Inspect', HarnessReply('Done', 'end_turn'), ())
    assert max(len(key.encode()) for key in client.objects) == 1024


def test_sdk_stubber_accepts_native_conditional_create_request():
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='offline', aws_secret_access_key='offline')
    with Stubber(client) as stub:
        stub.add_response('put_object', {'ETag': '"created"'},
            {'Bucket': BUCKET, 'Key': ANY, 'Body': ANY, 'ContentType': 'application/json', 'IfNoneMatch': '*'})
        stub.add_response('put_object', {'ETag': '"index"'},
            {'Bucket': BUCKET, 'Key': ANY, 'Body': ANY, 'ContentType': 'application/json', 'IfNoneMatch': '*'})
        task = make_store(client).create_task(context(), ARN, 'Inspect')
        assert task.etag == '"created"'
        stub.assert_no_pending_responses()
    client.close()


def test_storage_names_are_validated_without_sdk_or_credentials():
    from daeri.infrastructure.cloud_s3 import normalize_storage_prefix, validate_storage_bucket
    assert normalize_storage_prefix('company/daeri') == 'company/daeri/'
    assert normalize_storage_prefix('daeri/') == 'daeri/'
    assert validate_storage_bucket('company-daeri.archive') == 'company-daeri.archive'


@pytest.mark.parametrize('prefix', ['', '/', '/absolute', 'a//b', 'a/../b', 'a/./b', 'a\x00b', 'a\ud800'])
def test_invalid_storage_prefix_is_rejected(prefix):
    from daeri.infrastructure.cloud_s3 import normalize_storage_prefix
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        normalize_storage_prefix(prefix)


@pytest.mark.parametrize('bucket', ['ab', 'Uppercase', '127.0.0.1', 'a..b', 'a.-b', 'xn--bucket', 'bucket--x-s3'])
def test_invalid_storage_bucket_is_rejected(bucket):
    from daeri.infrastructure.cloud_s3 import validate_storage_bucket
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate_storage_bucket(bucket)


def approval_wait(client):
    store, task, turn = begin(client)
    pending = ({'toolUseId': 'plan-first', 'name': 'confirm_plan',
                'input': {'plan': 'Inspect the current project cache.'}, 'type': 'tool_use'},)
    store.finish_turn(task, turn, 'Inspect', HarnessReply('Review', 'tool_use', pending), ())
    return store, store.get_task(context(), task.id)


def test_native_plan_wait_is_durable_and_decision_is_bound_to_current_pending_plan():
    client = S3()
    store, task = approval_wait(client)
    assert getattr(task, 'approval', None) is not None
    assert task.approval.tool_use_id == 'plan-first' and task.approval.decision == 'pending'
    approved = store.record_decision(task, 'plan-first', task.approval.plan, 'approve', '승인합니다.')
    restored = make_store(client).get_task(context(), task.id)
    assert restored == approved and restored.approval.decision == 'approve'
    assert restored.approval.response == '승인합니다.' and restored.etag != task.etag
    for stale in (task, restored):
        with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
            store.record_decision(stale, 'plan-first', task.approval.plan, 'approve', '다시 승인')


@pytest.mark.parametrize('tool_id,plan,decision', [
    ('plan-old', 'Inspect the current project cache.', 'approve'),
    ('plan-first', 'Delete another project.', 'approve'),
    ('plan-first', 'Inspect the current project cache.', 'unknown'),
])
def test_mismatched_plan_or_decision_never_changes_the_waiting_approval(tool_id, plan, decision):
    client = S3()
    store, task = approval_wait(client)
    assert getattr(task, 'approval', None) is not None
    with pytest.raises(StoreError):
        store.record_decision(task, tool_id, plan, decision, '응답')
    assert store.get_task(context(), task.id) == task


def test_hook_lookup_requires_the_saved_harness_session_index_and_live_invocation():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect')
    assert hasattr(store, 'hook_get')
    index_key = 'daeri/sessions/' + sha256(ARN.encode()).hexdigest() + '/' + task.session_id + '.json'
    assert index_key in client.objects
    with pytest.raises(StoreError):
        store.hook_get(ARN, task.session_id)
    store.begin_turn(task, 'Inspect', ())
    assert store.hook_get(ARN, task.session_id).id == task.id
    with pytest.raises(StoreError):
        store.hook_get(ARN.replace('project-', 'another-'), task.session_id)
    raw, etag = client.objects[index_key]
    changed = json.loads(raw)
    changed['actor_id'] = 'bob'
    client.objects[index_key] = (json.dumps(changed).encode(), etag)
    with pytest.raises(StoreError):
        store.hook_get(ARN, task.session_id)


def test_missing_index_is_repaired_before_begin_and_conflicting_index_never_dispatches():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect')
    keys = [key for key in client.objects if '/sessions/' in key]
    assert len(keys) == 1
    saved = client.objects.pop(keys[0])
    store.begin_turn(task, 'Inspect', ())
    assert keys[0] in client.objects
    another = store.create_task(context(), ARN, 'Another')
    another_key = next(key for key in client.objects if key.endswith(another.session_id + '.json'))
    client.objects[another_key] = saved
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        store.begin_turn(another, 'Another', ())
    assert store._manifest(context(), another.id)[0]['in_flight'] is None


def test_approved_plan_is_cleared_by_new_normal_request_and_by_new_confirmation_hook():
    client = S3()
    store, task = approval_wait(client)
    assert getattr(task, 'approval', None) is not None
    task = store.record_decision(task, 'plan-first', task.approval.plan, 'approve', '승인')
    results = ({'toolUseId': 'plan-first', 'status': 'success', 'content': [{'text': 'Approved'}]},)
    store.begin_turn(task, '', results)
    active = store.hook_get(ARN, task.session_id)
    assert active.approval.decision == 'approve'
    store.revoke_approval(active)
    assert store.hook_get(ARN, task.session_id).approval is None
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        store.revoke_approval(active)


def test_new_text_turn_clears_previously_saved_approval_before_harness_invocation():
    client = S3()
    store, task = approval_wait(client)
    assert getattr(task, 'approval', None) is not None
    task = store.record_decision(task, 'plan-first', task.approval.plan, 'approve', '승인')
    results = ({'toolUseId': 'plan-first', 'status': 'success', 'content': [{'text': 'Approved'}]},)
    turn = store.begin_turn(task, '', results)
    store.finish_turn(task, turn, '', HarnessReply('Done', 'end_turn'), results)
    task = store.get_task(context(), task.id)
    store.begin_turn(task, 'Change the target.', ())
    assert store.hook_get(ARN, task.session_id).approval is None


def test_approval_survives_verified_pre_stream_rejection_but_not_uncertain_failure():
    client = S3()
    store, task = approval_wait(client)
    assert getattr(task, 'approval', None) is not None
    task = store.record_decision(task, 'plan-first', task.approval.plan, 'approve', '승인')
    results = ({'toolUseId': 'plan-first', 'status': 'success', 'content': [{'text': 'Approved'}]},)
    turn = store.begin_turn(task, '', results)
    store.finish_turn(task, turn, '', HarnessReply('Throttled', 'failed', task.pending_tools,
                                                 error='HARNESS_THROTTLED'), results)
    task = store.get_task(context(), task.id)
    assert task.approval.decision == 'approve'
    turn = store.begin_turn(task, '', results)
    store.finish_turn(task, turn, '', HarnessReply('Uncertain', 'failed', error='HARNESS_UNAVAILABLE'), results)
    assert store.get_task(context(), task.id).approval is None


def test_legacy_manifest_without_approval_still_loads_and_never_grants_approval():
    client = S3()
    store, task, _ = begin(client)
    key, value = manifest(client)
    value.pop('approval', None)
    client.objects[key] = (json.dumps(value).encode(), '"legacy"')
    loaded = store.get_task(context(), task.id)
    assert hasattr(loaded, 'approval') and loaded.approval is None


def test_task_listing_uses_scoped_native_pagination_and_rejects_cross_context_cursor():
    client = S3()
    store = make_store(client)
    created = [store.create_task(context(), ARN, goal) for goal in ('one', 'two', 'three')]
    store.create_task(AccessContext('bob', 'project'), ARN, 'Private')
    assert hasattr(store, 'list_tasks')
    page = store.list_tasks(context(), limit=2)
    assert len(page['items']) == 2 and page['next_cursor']
    second = store.list_tasks(context(), cursor=page['next_cursor'], limit=2)
    assert len(second['items']) == 1 and second['next_cursor'] is None
    assert {task.id for task in page['items'] + second['items']} == {task.id for task in created}
    request = next(request for op, request in client.calls if op == 'list')
    assert request['Delimiter'] == '/' and request['MaxKeys'] == 2
    for other, cursor in ((AccessContext('bob', 'project'), page['next_cursor']), (context(), 'not-a-cursor')):
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            store.list_tasks(other, cursor=cursor, limit=2)


def test_concurrent_user_decisions_only_commit_one_current_plan_response():
    client = S3()
    store, task = approval_wait(client)
    original_put, ready = client.put_object, Barrier(2)
    def simultaneous_put(**request):
        ready.wait(timeout=2)
        return original_put(**request)
    client.put_object = simultaneous_put
    def decide(choice):
        try:
            return store.record_decision(task, 'plan-first', task.approval.plan, choice, choice).approval.decision
        except StoreError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        choices = list(pool.map(decide, ('approve', 'reject')))
    assert choices.count('STORAGE_CONFLICT') == 1
    assert store.get_task(context(), task.id).approval.decision in {'approve', 'reject'}


def test_invalid_decision_type_is_a_controlled_input_error():
    client = S3()
    store, task = approval_wait(client)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        store.record_decision(task, 'plan-first', task.approval.plan, [], 'response')


def test_invalid_native_list_prefix_is_storage_failure_not_a_caller_error():
    client = S3()
    store = make_store(client)
    task = store.create_task(context(), ARN, 'Inspect')
    client.list_objects_v2 = lambda **_: {'CommonPrefixes': [{'Prefix': 'wrong/not-a-task/'}]}
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store.list_tasks(context())
    assert store.get_task(context(), task.id).goal == 'Inspect'


def test_live_invocation_snapshot_is_running_without_changing_persisted_state():
    client = S3()
    store, task, _ = begin(client)
    assert store.get_task(context(), task.id).state == 'running'
    assert manifest(client)[1]['state'] == 'active'


@pytest.mark.parametrize('pending', [[None], ['not-a-tool'], [{}], None])
def test_corrupt_pending_records_are_controlled_storage_errors(pending):
    client = S3()
    store, task, _ = begin(client)
    key, value = manifest(client)
    value['pending_tools'] = pending
    client.objects[key] = (json.dumps(value).encode(), '"corrupt"')
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        store.get_task(context(), task.id)
