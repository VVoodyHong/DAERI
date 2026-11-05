"""Raw native events and stored receipts rebuild a separate project Memory."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import boto3
from botocore.validate import validate_parameters
import pytest

from daeri.core.errors import StoreError
from daeri.core.cloud import AccessContext, HarnessReply
from daeri.infrastructure.project_deployment import memory_strategies
from test_cloud_s3 import S3, BUCKET


SOURCE, TARGET = 'Source-abcdefghij', 'Target-abcdefghij'
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def event(number, role='USER', actor='alice', session='session-1'):
    return {'memoryId': SOURCE, 'actorId': actor, 'sessionId': session,
            'eventId': f'{number}#abc', 'eventTimestamp': NOW.replace(second=number),
            'payload': [{'conversational': {'role': role, 'content': {'text': role + str(number)}}}]}


class Objects(S3):
    def list_objects_v2(self, **request):
        self.calls.append(('list', request))
        keys = sorted(key for key in self.objects if key.startswith(request['Prefix']))
        offset = int(request.get('ContinuationToken', '0'))
        end = offset + request['MaxKeys']
        result = {'Contents': [{'Key': key, 'LastModified': NOW} for key in keys[offset:end]],
                  'IsTruncated': end < len(keys)}
        if result['IsTruncated']:
            result['NextContinuationToken'] = str(end)
        return result


class Memory:
    def __init__(self):
        self.events = [event(2, 'TOOL'), event(1)]
        self.created, self.calls, self.tokens = [], [], {}
        self.model = boto3.Session()._session.get_service_model('bedrock-agentcore')

    def record(self, operation, request):
        validate_parameters(request, self.model.operation_model(operation).input_shape)
        self.calls.append((operation, deepcopy(request)))

    def list_actors(self, **request):
        self.record('ListActors', request)
        return {'actorSummaries': [{'actorId': 'alice'}] if request['memoryId'] == SOURCE else []}

    def list_sessions(self, **request):
        self.record('ListSessions', request)
        return {'sessionSummaries': [{'actorId': 'alice', 'sessionId': 'session-1', 'createdAt': NOW}]}

    def list_events(self, **request):
        self.record('ListEvents', request)
        if 'nextToken' not in request:
            return {'events': self.events[:1], 'nextToken': 'next'}
        return {'events': self.events[1:]}

    def create_event(self, **request):
        self.record('CreateEvent', request)
        token = request['clientToken']
        if token not in self.tokens:
            result = {key: value for key, value in request.items() if key not in {'clientToken'}}
            result['eventId'] = f'{len(self.tokens) + 100}#abc'
            self.tokens[token] = deepcopy(result)
            self.created.append(deepcopy(request))
        return {'event': deepcopy(self.tokens[token])}


class Control:
    def __init__(self):
        self.tags = {'daeri:project': 'project', 'daeri:catalog': '/daeri/projects/'}

    def get_memory(self, **request):
        strategies = []
        for kind, value in (next(iter(item.items())) for item in memory_strategies()):
            strategies.append({'name': value['name'], 'status': 'ACTIVE',
                'type': {'semanticMemoryStrategy': 'SEMANTIC', 'episodicMemoryStrategy': 'EPISODIC',
                         'summaryMemoryStrategy': 'SUMMARIZATION'}[kind],
                'namespaces': value['namespaces']})
            if kind == 'episodicMemoryStrategy':
                strategies[-1]['configuration'] = {'reflection': {
                    'episodicReflectionConfiguration': value['reflectionConfiguration']}}
        return {'memory': {'id': request['memoryId'], 'arn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/' + request['memoryId'],
                           'status': 'ACTIVE', 'strategies': strategies, 'eventExpiryDuration': 365}}

    def list_tags_for_resource(self, **request):
        return {'tags': deepcopy(self.tags)}


def setup():
    from daeri.infrastructure.memory_archive import MemoryArchive
    memory, objects, control = Memory(), Objects(), Control()
    return MemoryArchive(memory, objects, control, BUCKET), memory, objects, control


def test_preview_and_capture_include_all_native_pages_and_tool_payload_without_rewriting_events():
    archive, memory, objects, _ = setup()
    assert archive.capture('project', SOURCE)['events'] == 2
    assert not objects.objects
    archive.capture('project', SOURCE, apply=True)
    original = dict(objects.objects)
    archive.capture('project', SOURCE, apply=True)
    assert objects.objects == original and len(original) == 2
    assert all(call['includePayloads'] for name, call in memory.calls if name == 'ListEvents')
    stored = [json.loads(body) for body, etag in original.values()]
    assert {item['event']['payload'][0]['conversational']['role'] for item in stored} == {'USER', 'TOOL'}


def test_native_event_above_task_size_limit_roundtrips_without_raising_task_limit():
    archive, memory, objects, _ = setup()
    native = event(1)
    native['payload'] = [{'conversational': {'role': 'USER', 'content': {'text': 'x' * 100000}}}
                         for _ in range(90)]
    memory.events = [native]
    assert archive.capture('project', SOURCE, apply=True)['events'] == 1
    archive.capture('project', SOURCE, apply=True)
    assert len(objects.objects) == 1
    assert archive.replay('project', SOURCE, TARGET, apply=True)['replayed'] == 1
    assert memory.created[0]['payload'] == native['payload']
    assert memory.created[0]['eventTimestamp'] == native['eventTimestamp']
    assert archive.replay('project', SOURCE, TARGET, apply=True)['replayed'] == 0
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        archive.objects.create_task(AccessContext('alice', 'project'), 'saved-harness', 'x' * (8 * 1024 * 1024))


def test_replay_uses_fresh_memory_original_actor_session_time_and_idempotent_checkpoints():
    archive, memory, objects, _ = setup()
    archive.capture('project', SOURCE, apply=True)
    assert archive.replay('project', SOURCE, TARGET)['events'] == 2
    assert not memory.created
    result = archive.replay('project', SOURCE, TARGET, apply=True)
    assert result['replayed'] == 2 and result['extraction'] == 'asynchronous'
    assert [item['payload'][0]['conversational']['role'] for item in memory.created] == ['USER', 'TOOL']
    assert memory.created[0]['actorId'] == 'alice' and memory.created[0]['sessionId'] == 'session-1'
    assert memory.created[0]['eventTimestamp'] == event(1)['eventTimestamp']
    archive.replay('project', SOURCE, TARGET, apply=True)
    assert len(memory.created) == 2


def test_conflicting_original_event_is_not_overwritten():
    archive, memory, _, _ = setup()
    archive.capture('project', SOURCE, apply=True)
    memory.events[0]['payload'][0]['conversational']['content']['text'] = 'Different original'
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        archive.capture('project', SOURCE, apply=True)


def test_project_ownership_and_event_actor_mismatch_stop_capture():
    archive, memory, objects, control = setup()
    control.tags['daeri:project'] = 'other'
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        archive.capture('project', SOURCE, apply=True)
    assert not objects.objects
    control.tags['daeri:project'] = 'project'
    memory.events[0]['actorId'] = 'bob'
    with pytest.raises(StoreError, match='MEMORY_ARCHIVE_INVALID'):
        archive.capture('project', SOURCE, apply=True)
    assert not objects.objects


def test_existing_target_and_source_target_reuse_are_rejected():
    archive, memory, _, _ = setup()
    archive.capture('project', SOURCE, apply=True)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        archive.replay('project', SOURCE, SOURCE, apply=True)
    memory.list_actors = lambda **request: {'actorSummaries': [{'actorId': 'existing'}]}
    with pytest.raises(StoreError, match='TARGET_NOT_EMPTY'):
        archive.replay('project', SOURCE, TARGET, apply=True)
    assert not memory.created


def test_branch_roots_are_mapped_to_replayed_event_ids():
    archive, memory, _, _ = setup()
    memory.events[0]['branch'] = {'name': 'alternative', 'rootEventId': '1#abc'}
    archive.capture('project', SOURCE, apply=True)
    archive.replay('project', SOURCE, TARGET, apply=True)
    assert memory.created[1]['branch'] == {'name': 'alternative', 'rootEventId': '100#abc'}


def test_receipt_fallback_rebuilds_original_requests_tool_evidence_and_result_after_native_expiry():
    archive, memory, objects, _ = setup()
    task = archive.objects.create_task(AccessContext('alice', 'project'), 'saved-harness', 'Compare')
    turn = archive.objects.begin_turn(task, 'Compare the counts', ())
    archive.objects.finish_turn(task, turn, 'Compare the counts',
        HarnessReply('Verified total', 'end_turn', observations=({'count': 42},)), ())
    preview = archive.capture_receipts('project')
    assert preview['events'] == 1 and not any('/memory/' in key for key in objects.objects)
    archive.capture_receipts('project', apply=True)
    archive.replay('project', 'receipts', TARGET, apply=True)
    messages = [item['conversational'] for event in memory.created for item in event['payload']]
    assert [message['role'] for message in messages] == ['USER', 'TOOL', 'ASSISTANT']
    assert memory.created[0]['sessionId'] == task.session_id
    assert '42' in messages[1]['content']['text']


def test_receipt_fallback_rebuilds_the_full_offloaded_response_and_tool_evidence():
    archive, memory, _, _ = setup()
    task = archive.objects.create_task(AccessContext('alice', 'project'), 'saved-harness', 'Compare')
    turn = archive.objects.begin_turn(task, 'Compare the counts', ())
    text = 'Verified total: ' + 'x' * (8 * 1024 * 1024) + ' complete.'
    evidence = {'toolUseId': 'query', 'status': 'success', 'content': [{'json': {'count': 42}}]}
    archive.objects.finish_turn(task, turn, 'Compare the counts',
        HarnessReply(text, 'end_turn', observations=(evidence,)), ())

    archive.capture_receipts('project', apply=True)
    archive.replay('project', 'receipts', TARGET, apply=True)

    messages = [item['conversational'] for event in memory.created for item in event['payload']]
    assert messages[0] == {'role': 'USER', 'content': {'text': 'Compare the counts'}}
    assert json.loads(messages[1]['content']['text']) == evidence
    assert ''.join(message['content']['text'] for message in messages if message['role'] == 'ASSISTANT') == text
    assert all(item['actorId'] == 'alice' and item['sessionId'] == task.session_id for item in memory.created)


@pytest.mark.parametrize('text', [' \t\n' + '검🙂e\u0301\t' * 20000 + '\n\t ',
                                'x' * 10000000, '\x00' * 1800000],
                         ids=['unicode_whitespace', 'message_count', 'encoded_bytes'])
def test_receipt_payloads_preserve_text_within_native_message_and_event_limits(text):
    from daeri.infrastructure.memory_archive import receipt_events
    from daeri.core.serialization import validate_storable_json
    receipt = {'prompt': 'Review', 'reply': {'text': text, 'observations': []}, 'tool_results': [],
               'actor_id': 'alice', 'session_id': 'session-1',
               'turn_id': '00000000-0000-0000-0000-000000000001'}
    events = list(receipt_events(receipt, NOW))
    messages = [item['conversational'] for event in events for item in event['payload']]
    assert all(0 < len(message['content']['text'].encode('utf-8')) <= 100000 for message in messages)
    assert all(1 <= len(event['payload']) <= 100 and len(validate_storable_json(event)) <= 10000000
               for event in events)
    assert {event['eventTimestamp'] for event in events} == {'2026-11-01T00:00:00.000000+00:00'}
    assert messages[0] == {'role': 'USER', 'content': {'text': 'Review'}}
    assert ''.join(message['content']['text'] for message in messages if message['role'] == 'ASSISTANT') == text


def test_receipt_replay_keeps_tied_timestamp_turns_and_large_batches_together():
    archive, memory, objects, _ = setup()
    context = AccessContext('alice', 'project')
    task = archive.objects.create_task(context, 'saved-harness', 'Review')
    turns = []
    for label in ('A', 'B'):
        turn = archive.objects.begin_turn(task, 'Review ' + label, ())
        archive.objects.finish_turn(task, turn, 'Review ' + label,
                                    HarnessReply(label * 10000000, 'end_turn'), ())
        turns.append(turn)
        task = archive.objects.get_task(context, task.id)
    assert archive.capture_receipts('project', apply=True)['events'] == 4
    snapshot = dict(objects.objects)
    archive.capture_receipts('project', apply=True)
    assert objects.objects == snapshot
    assert archive.replay('project', 'receipts', TARGET, apply=True)['replayed'] == 4
    groups = [event['metadata']['daeri_turn']['stringValue'] for event in memory.created]
    assert groups in [[turns[0], turns[0], turns[1], turns[1]], [turns[1], turns[1], turns[0], turns[0]]]
    for turn, label in zip(turns, ('A', 'B')):
        messages = [item['conversational'] for event in memory.created
                    if event['metadata']['daeri_turn']['stringValue'] == turn for item in event['payload']]
        assert messages[0] == {'role': 'USER', 'content': {'text': 'Review ' + label}}
        assert ''.join(message['content']['text'] for message in messages[1:]) == label * 10000000
    assert archive.replay('project', 'receipts', TARGET, apply=True)['replayed'] == 0


def test_receipt_collection_does_not_mix_legacy_archives_or_reuse_their_restore_target():
    archive, memory, objects, _ = setup()
    native = archive._scope('project', SOURCE, '/daeri/projects/')
    receipts = archive._scope('project', 'receipts', '/daeri/projects/')
    assert archive._root(native).endswith('/events/')
    assert archive._root(receipts).endswith('/receipt-events-v2/')
    legacy = archive._root(receipts).replace('/receipt-events-v2/', '/events/')
    objects.put_object(Bucket=BUCKET, Key=legacy + 'old.json', Body=b'{}')
    with pytest.raises(StoreError, match='ARCHIVE_NOT_FOUND'):
        archive.replay('project', 'receipts', TARGET, apply=True)
    task = archive.objects.create_task(AccessContext('alice', 'project'), 'saved-harness', 'Review')
    turn = archive.objects.begin_turn(task, 'Review', ())
    archive.objects.finish_turn(task, turn, 'Review', HarnessReply('Done', 'end_turn'), ())
    archive.capture_receipts('project', apply=True)
    archive.replay('project', 'receipts', TARGET, apply=True)
    key = next(key for key in objects.objects if '/restore/' in key and key.endswith('/manifest.json'))
    binding = archive._read(key)
    binding.pop('collection')
    archive.objects._put(key, binding)
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        archive.replay('project', 'receipts', TARGET, apply=True)
    assert len(memory.created) == 1 and objects.objects[legacy + 'old.json'][0] == b'{}'


def test_new_receipt_capture_cycle_does_not_resume_a_legacy_collection_cursor():
    archive, _, objects, _ = setup()
    task = archive.objects.create_task(AccessContext('alice', 'project'), 'saved-harness', 'Review')
    turn = archive.objects.begin_turn(task, 'Review', ())
    archive.objects.finish_turn(task, turn, 'Review', HarnessReply('Done', 'end_turn'), ())
    scope = archive._scope('project', 'receipts', '/daeri/projects/')
    legacy = archive._root(scope).replace('/receipt-events-v2/', '/') + 'capture.json'
    archive.objects._put(legacy, {**scope, 'events': 9, 'pending': [{'kind': 'receipts', 'token': '999'}]})
    previous = objects.objects[legacy]

    result = archive.capture_batch('project', 'receipts')

    assert result['complete'] and result['events'] == 1
    assert len(list(archive._keys(archive._root(scope)))) == 1
    assert objects.objects[legacy] == previous


def test_replay_reuses_client_token_when_checkpoint_write_failed_after_event_creation(monkeypatch):
    archive, memory, _, _ = setup()
    archive.capture('project', SOURCE, apply=True)
    original = archive._immutable
    fail = [True]
    def interrupted(key, document):
        if 'event_id' in document and fail[0]:
            fail[0] = False
            raise StoreError('STORAGE_UNAVAILABLE', 'Interrupted checkpoint')
        return original(key, document)
    monkeypatch.setattr(archive, '_immutable', interrupted)
    with pytest.raises(StoreError, match='STORAGE_UNAVAILABLE'):
        archive.replay('project', SOURCE, TARGET, apply=True)
    archive.replay('project', SOURCE, TARGET, apply=True)
    assert len(memory.created) == 2
    tokens = [request['clientToken'] for method, request in memory.calls if method == 'CreateEvent']
    assert tokens[0] == tokens[1]


def test_repeated_cursor_and_partial_page_failure_are_reported_and_can_be_retried(monkeypatch):
    archive, memory, objects, _ = setup()
    original = memory.list_events
    monkeypatch.setattr(memory, 'list_events', lambda **request: {'events': memory.events[:1], 'nextToken': 'same'})
    with pytest.raises(StoreError, match='cursor did not advance'):
        archive.capture('project', SOURCE, apply=True)
    assert len(objects.objects) == 1
    monkeypatch.setattr(memory, 'list_events', original)
    archive.capture('project', SOURCE, apply=True)
    assert len(objects.objects) == 2


@pytest.mark.parametrize('change', ['reflection', 'expiry', 'record_expiry', 'extra_strategy'])
def test_rebuild_target_requires_the_same_project_memory_contract_as_publication(change):
    archive, memory, _, control = setup()
    original = control.get_memory
    def changed(**request):
        result = original(**request)
        if change == 'reflection':
            result['memory']['strategies'][1]['configuration'] = {}
        elif change == 'expiry':
            result['memory']['eventExpiryDuration'] = 30
        elif change == 'record_expiry':
            result['memory']['strategies'][0]['recordExpiryDurationDays'] = 60
        else:
            result['memory']['strategies'].append({'name': 'Extra'})
        return result
    control.get_memory = changed
    with pytest.raises(StoreError):
        archive.replay('project', SOURCE, TARGET, apply=True)
    assert not memory.created


def test_foreign_archive_body_and_reused_target_binding_stop_without_replaying():
    archive, memory, objects, _ = setup()
    archive.capture('project', SOURCE, apply=True)
    key = sorted(objects.objects)[0]
    document = archive._read(key)
    document['project_id'] = 'other'
    objects.objects[key] = (json.dumps(document).encode(), 'tampered')
    with pytest.raises(StoreError, match='another project'):
        archive.replay('project', SOURCE, TARGET, apply=True)
    assert not memory.created
    receipts = archive._scope('project', 'receipts', '/daeri/projects/')
    copy = {**document, **receipts, 'event': {**document['event'], 'memoryId': 'receipts',
            'eventId': '00000000-0000-0000-0000-000000000001-0000000000'}}
    archive._save(receipts, copy, True)
    with pytest.raises(StoreError, match='different reconstruction'):
        archive.replay('project', 'receipts', TARGET, apply=True)


def test_receipt_identity_mismatch_cannot_change_the_replayed_actor():
    archive, memory, objects, _ = setup()
    task = archive.objects.create_task(AccessContext('alice', 'project'), 'saved-harness', 'Compare')
    turn = archive.objects.begin_turn(task, 'Compare', ())
    archive.objects.finish_turn(task, turn, 'Compare', HarnessReply('Done', 'end_turn'), ())
    key = next(key for key in objects.objects if '/turns/' in key)
    document = archive._read(key)
    document['actor_id'] = 'bob'
    objects.objects[key] = (json.dumps(document).encode(), 'tampered')
    with pytest.raises(StoreError, match='changed its project, actor or task'):
        archive.capture_receipts('project', apply=True)
    assert not memory.created


def test_scheduled_capture_uses_only_deployment_settings_and_fails_invalid_config_before_aws(monkeypatch):
    from contextlib import contextmanager
    from daeri.infrastructure.memory_archive import lambda_handler
    calls = []
    @contextmanager
    def archive_builder(settings):
        calls.append(settings)
        class Capture:
            def capture_batch(self, *args, **kwargs):
                calls.append((args, kwargs))
                return {'events': 2, 'archived': True}
        yield Capture()
    monkeypatch.setattr('daeri.config.memory.build_memory_archive', archive_builder)
    monkeypatch.setenv('DAERI_MEMORY_ARCHIVE_CONFIG', json.dumps({
        'project_id': 'project', 'source_memory_id': SOURCE, 'storage': {'bucket': BUCKET}}))
    assert lambda_handler({'project_id': 'other'}, None)['events'] == 2
    assert calls[-1] == (('project', SOURCE), {'catalog': '/daeri/projects/'})
    calls.clear()
    monkeypatch.setenv('DAERI_MEMORY_ARCHIVE_CONFIG', '{invalid')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        lambda_handler({}, None)
    assert not calls


def test_same_timestamp_branch_child_is_deferred_until_its_root_is_replayed():
    archive, memory, objects, _ = setup()
    root = event(1)
    child = event(2, 'TOOL')
    child['eventTimestamp'] = root['eventTimestamp']
    child['branch'] = {'name': 'alternate', 'rootEventId': root['eventId']}
    memory.events = [child, root]
    archive.capture('project', SOURCE, apply=True)
    # Force the adverse tie order independently of the hash implementation.
    original = archive._keys
    archive._keys = lambda prefix: iter(sorted(original(prefix),
        key=lambda item: not bool(archive._read(item[0])['event'].get('branch'))))
    saved = dict(objects.objects)
    assert archive.replay('project', SOURCE, TARGET)['events'] == 2
    assert objects.objects == saved and not memory.created
    archive.replay('project', SOURCE, TARGET, apply=True)
    assert [item['payload'][0]['conversational']['role'] for item in memory.created] == ['USER', 'TOOL']
    assert memory.created[1]['branch']['rootEventId'] == '100#abc'


def test_missing_archive_or_branch_root_stops_without_claiming_history_was_rebuilt():
    archive, memory, objects, _ = setup()
    with pytest.raises(StoreError, match='ARCHIVE_NOT_FOUND'):
        archive.replay('project', SOURCE, TARGET, apply=True)
    memory.events[0]['branch'] = {'name': 'alternate', 'rootEventId': '9#fff'}
    archive.capture('project', SOURCE, apply=True)
    saved = dict(objects.objects)
    with pytest.raises(StoreError, match='missing a branch root'):
        archive.replay('project', SOURCE, TARGET)
    assert objects.objects == saved and not memory.created
    with pytest.raises(StoreError, match='missing a branch root'):
        archive.replay('project', SOURCE, TARGET, apply=True)
    assert len(memory.created) == 1


def test_preview_detects_a_branch_cycle_without_changing_archive_or_target():
    archive, memory, objects, _ = setup()
    memory.events[0]['branch'] = {'name': 'first', 'rootEventId': memory.events[1]['eventId']}
    memory.events[1]['branch'] = {'name': 'second', 'rootEventId': memory.events[0]['eventId']}
    archive.capture('project', SOURCE, apply=True)
    saved = dict(objects.objects)
    with pytest.raises(StoreError, match='branch cycle'):
        archive.replay('project', SOURCE, TARGET)
    assert objects.objects == saved and not memory.created


def test_scheduled_capture_progress_resumes_across_bounded_runs_and_completed_cycles():
    archive, _, objects, _ = setup()
    for _ in range(3):
        assert not archive.capture_batch('project', SOURCE, max_pages=1)['complete']
    result = archive.capture_batch('project', SOURCE, max_pages=1)
    assert result['complete'] and result['events'] == 2
    count = len(objects.objects)
    assert archive.capture_batch('project', SOURCE)['complete']
    assert len(objects.objects) == count


def test_scheduled_receipt_collection_also_advances_in_bounded_steps():
    archive, _, _, _ = setup()
    task = archive.objects.create_task(AccessContext('alice', 'project'), 'saved-harness', 'Compare')
    turn = archive.objects.begin_turn(task, 'Compare', ())
    archive.objects.finish_turn(task, turn, 'Compare', HarnessReply('Done', 'end_turn'), ())
    assert not archive.capture_batch('project', 'receipts', max_pages=1)['complete']
    result = archive.capture_batch('project', 'receipts')
    assert result['complete'] and result['events'] == 1


def test_archive_command_rejects_invalid_memory_id_before_building_clients(tmp_path, monkeypatch, capsys):
    from scripts.archive_memory import main
    monkeypatch.setattr('scripts.archive_memory.build_memory_archive', lambda settings: pytest.fail('Unexpected AWS client'))
    path = tmp_path / 'memory.yaml'
    path.write_text('project_id: project\nsource_memory_id: invalid/private-value\nstorage:\n  bucket: evidence-bucket\n')
    assert main(['--config', str(path)]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)['error']['code'] == 'INVALID_INPUT' and 'private-value' not in output
