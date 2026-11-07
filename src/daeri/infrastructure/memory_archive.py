"""Immutable raw Memory events and resumable re-extraction into a separate Memory."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from itertools import chain
import json
import re
from time import monotonic
from uuid import UUID

from botocore.exceptions import PaginationError
from botocore.session import get_session
from botocore.validate import validate_parameters

from daeri.core.errors import StoreError
from daeri.core.projects import valid_harness_release, valid_project_key
from daeri.core.serialization import validate_storable_json
from daeri.infrastructure.aws_pagination import pages
from daeri.infrastructure.cloud_s3 import S3CloudStore
from daeri.infrastructure.project_catalog import normalize_project_prefix
from daeri.infrastructure.project_deployment import validate_project_memory


_MAX_ARCHIVE_BYTES = 10 * 1024 * 1024 + 2048  # Native event quota plus project/source envelope.
_RECEIPT_COLLECTION = 'receipt-events-v2'


def _hash(value):
    return sha256(validate_storable_json(value)).hexdigest()


def _memory_id(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'(?:arn:(?:aws|aws-cn|aws-us-gov):bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:memory/)?'
            r'[A-Za-z][A-Za-z0-9_-]{0,99}-[A-Za-z0-9]{10}', value):
        raise StoreError('INVALID_INPUT', 'Supply an AgentCore Memory identifier.')
    return value.rsplit('/', 1)[-1]


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archived event needs an explicit UTC-compatible timestamp.')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def receipt_events(receipt, recorded_at):
    """Approximate native-expired conversation from immutable input, evidence and response."""
    timestamp = _timestamp(recorded_at)
    if (not isinstance(receipt['prompt'], str) or not isinstance(receipt['reply']['text'], str)
            or not isinstance(receipt['tool_results'], list) or not isinstance(receipt['reply']['observations'], list)):
        raise StoreError('MEMORY_ARCHIVE_INVALID', 'The stored turn cannot be converted to Memory events.')
    messages = [('USER', receipt['prompt'])]
    messages += [('TOOL', json.dumps(value, ensure_ascii=False, allow_nan=False))
                 for value in receipt['tool_results'] + receipt['reply']['observations']]
    messages.append(('ASSISTANT', receipt['reply']['text']))
    event = {'memoryId': 'receipts', 'actorId': receipt['actor_id'], 'sessionId': receipt['session_id'],
             'eventTimestamp': timestamp,
             'metadata': {'daeri_source': {'stringValue': 'receipt'},
                          'daeri_turn': {'stringValue': receipt['turn_id']}}}
    payload, size, index = [], 0, 0
    for role, text in messages:
        data, start = text.encode('utf-8'), 0
        while start < len(data):
            end = min(start + 100000, len(data))
            while end < len(data) and data[end] & 0xc0 == 0x80:
                end -= 1
            message = {'conversational': {'role': role, 'content': {'text': data[start:end].decode('utf-8')}}}
            message_size = len(validate_storable_json(message)) + 1
            # Reserve the event envelope while bounding escaped JSON as well as message count.
            if payload and (len(payload) == 100 or size + message_size > 10000000 - 2048):
                yield {**event, 'eventId': receipt['turn_id'] + f'-{index:010d}', 'payload': payload}
                payload, size, index = [], 0, index + 1
            payload.append(message)
            size += message_size
            start = end
    if payload:
        yield {**event, 'eventId': receipt['turn_id'] + f'-{index:010d}', 'payload': payload}


class MemoryArchive:
    def __init__(self, memory, s3, control, bucket, prefix='daeri/'):
        self.memory, self.s3, self.control = memory, s3, control
        self.objects = S3CloudStore(s3, bucket, prefix)
        self.create_shape = get_session().get_service_model('bedrock-agentcore').operation_model('CreateEvent').input_shape

    def _scope(self, project_id, source_memory_id, catalog):
        if not valid_project_key(project_id):
            raise StoreError('INVALID_INPUT', 'Supply a project identifier.')
        source = source_memory_id if source_memory_id == 'receipts' else _memory_id(source_memory_id)
        return {'project_id': project_id, 'catalog': normalize_project_prefix(catalog), 'source_memory_id': source}

    def _root(self, scope):
        collection = _RECEIPT_COLLECTION if scope['source_memory_id'] == 'receipts' else 'events'
        return self.objects.prefix + 'memory/' + _hash(scope) + '/' + collection + '/'

    def _owned(self, memory_id, scope, *, target=False):
        resource = self.control.get_memory(memoryId=memory_id)['memory']
        tags = self.control.list_tags_for_resource(resourceArn=resource['arn']).get('tags', {})
        if (resource.get('status') != 'ACTIVE' or resource.get('managedByResourceArn')
                or tags.get('daeri:project') != scope['project_id'] or tags.get('daeri:catalog') != scope['catalog']):
            raise StoreError('ACCESS_DENIED', 'The Memory resource is not independently owned by this project.')
        if target:
            validate_project_memory(resource, tags, scope['project_id'], scope['catalog'])
        return resource

    def _pages(self, method, field, **request):
        try:
            for response in pages(self.memory, method, maxResults=100, **request):
                values = response[field]
                if not isinstance(values, list):
                    raise StoreError('MEMORY_ARCHIVE_INVALID', 'The Memory page is invalid.')
                yield from values
        except PaginationError:
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The Memory cursor did not advance.') from None
        except TypeError:
            # The paginator measures the result field before yielding the page.
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The Memory page is invalid.') from None

    def _keys(self, prefix):
        try:
            for response in pages(self.s3, 'list_objects_v2', token_field='NextContinuationToken',
                                  Bucket=self.objects.bucket, Prefix=prefix, MaxKeys=100):
                if response.get('IsTruncated') and not response.get('NextContinuationToken'):
                    raise PaginationError(message='The truncated archive page is missing its cursor.')
                for item in response.get('Contents', []):
                    key = item['Key']
                    if not isinstance(key, str) or not key.startswith(prefix):
                        raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archive page is outside its scope.')
                    yield key, item
        except PaginationError:
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archive cursor did not advance.') from None

    def _read(self, key, *, max_bytes=None):
        try:
            options = {} if max_bytes is None else {'max_bytes': max_bytes}
            return self.objects._get(key, 'ARCHIVE_NOT_FOUND', **options)[0]
        except StoreError as error:
            if error.code == 'ARCHIVE_NOT_FOUND':
                return None
            raise

    def _immutable(self, key, document, *, max_bytes=None):
        options = {} if max_bytes is None else {'max_bytes': max_bytes}
        try:
            self.objects._put(key, document, IfNoneMatch='*', **options)
        except StoreError as error:
            if error.code != 'STORAGE_CONFLICT' or self._read(key, max_bytes=max_bytes) != document:
                raise

    def _event(self, scope, event, actor=None, session=None):
        value = deepcopy(event)
        if (_memory_id(value['memoryId']) != scope['source_memory_id']
                or actor is not None and value['actorId'] != actor
                or session is not None and value['sessionId'] != session
                or not isinstance(value['eventId'], str) or not value['eventId']):
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The event does not match the requested project session.')
        validate_parameters({key: value[key] for key in ('memoryId', 'actorId', 'sessionId', 'eventTimestamp', 'payload')},
                            self.create_shape)
        value['eventTimestamp'] = _timestamp(value['eventTimestamp'])
        validate_storable_json(value)
        return {**scope, 'event': value}

    def _event_key(self, scope, document):
        event = document['event']
        session = _hash([event['actorId'], event['sessionId']])[:32]
        timestamp = event['eventTimestamp'].replace(':', '').replace('+', '')
        suffix = _hash(event['eventId'])[:32]
        if scope['source_memory_id'] == 'receipts':
            turn, _, batch = event['eventId'].rpartition('-')
            if (not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', turn)
                    or not re.fullmatch(r'[0-9]{10}', batch)):
                raise StoreError('MEMORY_ARCHIVE_INVALID', 'The receipt batch identity is invalid.')
            suffix = turn + '_' + batch
        return self._root(scope) + session + '/' + timestamp + '_' + suffix + '.json'

    def _save(self, scope, document, apply):
        if apply:
            self._immutable(self._event_key(scope, document), document, max_bytes=_MAX_ARCHIVE_BYTES)

    def _session(self, scope, actor_id, session_id, apply):
        count = 0
        for event in self._pages('list_events', 'events', memoryId=scope['source_memory_id'],
                                 actorId=actor_id, sessionId=session_id, includePayloads=True):
            self._save(scope, self._event(scope, event, actor_id, session_id), apply)
            count += 1
        return count

    def _receipts_root(self, scope):
        return self.objects.prefix + 'projects/' + sha256(scope['project_id'].encode()).hexdigest() + '/actors/'

    def _receipt(self, scope, key, recorded_at):
        if not key.startswith(self._receipts_root(scope)):
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The stored turn is outside its project archive.')
        relative = key[len(self._receipts_root(scope)):].split('/')
        if len(relative) != 5 or relative[1] != 'tasks' or relative[3] != 'turns' or not relative[4].endswith('.json'):
            return
        receipt = self._read(key)
        if (receipt['project_id'] != scope['project_id'] or sha256(receipt['actor_id'].encode()).hexdigest() != relative[0]
                or str(UUID(receipt['task_id'])) != relative[2]
                or str(UUID(receipt['turn_id'])) + '.json' != relative[4]):
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The stored turn changed its project, actor or task identity.')
        binding = self._read(self.objects._session_key(receipt['harness_arn'], receipt['session_id']))
        expected = {k: receipt[k] for k in ('harness_arn', 'session_id', 'task_id', 'actor_id', 'project_id')}
        endpoint, version = receipt.get('harness_endpoint', ''), receipt.get('harness_version', '')
        if binding != expected or ((endpoint or version) and not valid_harness_release(endpoint, version)):
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The stored turn changed its original harness session or release.')
        receipt = self.objects.read_original_receipt(receipt)
        for event in receipt_events(receipt, recorded_at):
            validate_parameters({k: event[k] for k in ('actorId', 'sessionId', 'payload')}
                | {'memoryId': 'Receipts-0000000000', 'eventTimestamp': datetime.fromisoformat(event['eventTimestamp'])},
                self.create_shape)
            yield {**scope, 'event': event}

    def capture(self, project_id, source_memory_id, *, catalog='/daeri/projects/', apply=False):
        scope = self._scope(project_id, source_memory_id, catalog)
        try:
            self._owned(scope['source_memory_id'], scope)
            count = 0
            for actor in self._pages('list_actors', 'actorSummaries', memoryId=scope['source_memory_id']):
                for session in self._pages('list_sessions', 'sessionSummaries',
                                          memoryId=scope['source_memory_id'], actorId=actor['actorId']):
                    if session['actorId'] != actor['actorId']:
                        raise StoreError('MEMORY_ARCHIVE_INVALID', 'The session page changed actor identity.')
                    count += self._session(scope, actor['actorId'], session['sessionId'], apply)
            return {'events': count, 'archived': apply, 'coverage': 'available_raw_events_only'}
        except StoreError:
            raise
        except Exception:
            raise StoreError('MEMORY_ARCHIVE_FAILED', 'Raw Memory events could not be archived; retry before event expiry.') from None

    def capture_receipts(self, project_id, *, catalog='/daeri/projects/', apply=False):
        scope = self._scope(project_id, 'receipts', catalog)
        count = 0
        try:
            for key, metadata in self._keys(self._receipts_root(scope)):
                for document in self._receipt(scope, key, metadata['LastModified']):
                    self._save(scope, document, apply)
                    count += 1
            return {'events': count, 'archived': apply, 'source': 'receipts', 'coverage': 'saved_turns_only'}
        except StoreError:
            raise
        except Exception:
            raise StoreError('MEMORY_ARCHIVE_FAILED', 'Stored turn evidence could not be archived for reconstruction.') from None

    def _capture_page(self, scope, task):
        kind = task['kind']
        allowed = {'receipts', 'receipt'} if scope['source_memory_id'] == 'receipts' else {'actors', 'sessions', 'events'}
        if kind not in allowed:
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The capture page changed its source type.')
        if kind == 'receipt':
            count = 0
            for document in self._receipt(scope, task['key'], datetime.fromisoformat(task['recorded_at'])):
                self._save(scope, document, True)
                count += 1
            return [], count
        request = {'maxResults': 100, 'memoryId': scope['source_memory_id']}
        if task.get('token'):
            request['nextToken'] = task['token']
        if kind == 'receipts':
            request = {'Bucket': self.objects.bucket, 'Prefix': self._receipts_root(scope), 'MaxKeys': 100}
            if task.get('token'):
                request['ContinuationToken'] = task['token']
            response = self.s3.list_objects_v2(**request)
            items, token = response.get('Contents', []), response.get('NextContinuationToken')
            if bool(response.get('IsTruncated')) != bool(token):
                raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archive cursor is invalid.')
            children = []
            for item in items:
                if not item['Key'].startswith(request['Prefix']):
                    raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archive page is outside its project.')
                children.append({'kind': 'receipt', 'key': item['Key'], 'recorded_at': _timestamp(item['LastModified'])})
            count = 0
        else:
            if 'actor' in task:
                request['actorId'] = task['actor']
            if kind == 'events':
                request.update(sessionId=task['session'], includePayloads=True, maxResults=10)
            operation, field = {'actors': ('list_actors', 'actorSummaries'),
                'sessions': ('list_sessions', 'sessionSummaries'), 'events': ('list_events', 'events')}[kind]
            response = getattr(self.memory, operation)(**request)
            items, token, children, count = response[field], response.get('nextToken'), [], 0
            for item in items:
                if kind == 'actors':
                    children.append({'kind': 'sessions', 'actor': item['actorId']})
                elif kind == 'sessions':
                    if item['actorId'] != task['actor']:
                        raise StoreError('MEMORY_ARCHIVE_INVALID', 'The session page changed actor identity.')
                    children.append({'kind': 'events', 'actor': task['actor'], 'session': item['sessionId']})
                else:
                    self._save(scope, self._event(scope, item, task['actor'], task['session']), True)
                    count += 1
        if token:
            seen = task.get('seen', []) + ([task['token']] if task.get('token') else [])
            if not isinstance(token, str) or token in seen:
                raise StoreError('MEMORY_ARCHIVE_INVALID', 'The capture cursor did not advance.')
            children.append({**task, 'token': token, 'seen': seen})
        return children, count

    def capture_batch(self, project_id, source_memory_id, *, catalog='/daeri/projects/', max_pages=50):
        """Scheduled capture advances a persisted queue, including across Lambda time windows."""
        scope = self._scope(project_id, source_memory_id, catalog)
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise StoreError('INVALID_INPUT', 'Select a bounded number of capture pages.')
        try:
            if source_memory_id != 'receipts':
                self._owned(scope['source_memory_id'], scope)
            filename = 'capture-' + _RECEIPT_COLLECTION + '.json' if source_memory_id == 'receipts' else 'capture.json'
            key = self.objects.prefix + 'memory/' + _hash(scope) + '/' + filename
            try:
                state, etag = self.objects._get(key, 'ARCHIVE_NOT_FOUND')
            except StoreError as error:
                if error.code != 'ARCHIVE_NOT_FOUND':
                    raise
                state, etag = None, None
            if state is not None and ({k: state.get(k) for k in scope} != scope or not isinstance(state.get('pending'), list)):
                raise StoreError('MEMORY_ARCHIVE_INVALID', 'The saved capture cursor changed project or source.')
            if state is None or not state['pending']:
                state = {**scope, 'events': 0, 'pending': [{'kind': 'receipts' if source_memory_id == 'receipts' else 'actors'}]}
            deadline = monotonic() + 45
            for _ in range(max_pages):
                children, count = self._capture_page(scope, state['pending'][0])
                state = {**state, 'events': state['events'] + count, 'pending': children + state['pending'][1:]}
                etag = self.objects._put(key, state, **({'IfMatch': etag} if etag else {'IfNoneMatch': '*'}))
                if not state['pending'] or monotonic() >= deadline:
                    break
            return {'events': state['events'], 'archived': True, 'complete': not state['pending'],
                    'coverage': 'saved_turns_only' if source_memory_id == 'receipts' else 'available_raw_events_only'}
        except StoreError:
            raise
        except Exception:
            raise StoreError('MEMORY_ARCHIVE_FAILED', 'Scheduled capture stopped; retry to resume its saved page.') from None

    def replay(self, project_id, source_memory_id, target_memory_id, *, catalog='/daeri/projects/', apply=False):
        scope = self._scope(project_id, source_memory_id, catalog)
        target = _memory_id(target_memory_id)
        if target == scope['source_memory_id']:
            raise StoreError('INVALID_INPUT', 'Rebuild into a separate fresh Memory resource.')
        try:
            self._owned(target, scope, target=True)
            keys = self._keys(self._root(scope))
            first = next(keys, None)
            if first is None:
                raise StoreError('ARCHIVE_NOT_FOUND', 'Archive the available raw events or saved turns before reconstructing Memory.')
            restore = self.objects.prefix + 'restore/' + _hash([project_id, scope['catalog'], target]) + '/'
            binding = {**scope, 'target_memory_id': target}
            if scope['source_memory_id'] == 'receipts':
                binding['collection'] = _RECEIPT_COLLECTION
            old = self._read(restore + 'manifest.json')
            if old is not None and old != binding:
                raise StoreError('STORAGE_CONFLICT', 'The target Memory belongs to a different reconstruction.')
            if old is None:
                if self.memory.list_actors(memoryId=target, maxResults=1)['actorSummaries']:
                    raise StoreError('TARGET_NOT_EMPTY', 'Choose a fresh Memory resource for reconstruction.')
                if apply:
                    self._immutable(restore + 'manifest.json', binding)
            count, replayed, pending = 0, 0, []
            previewed = set() if not apply else None
            for key, _ in chain([first], keys):
                ready, created = self._replay_event(scope, binding, restore, key, apply, previewed)
                count += 1
                replayed += created
                if not ready:
                    pending.append(key)
            while pending:
                remaining = []
                for key in pending:
                    ready, created = self._replay_event(scope, binding, restore, key, apply, previewed)
                    replayed += created
                    if not ready:
                        remaining.append(key)
                if len(remaining) == len(pending):
                    raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archive is missing a branch root or contains a branch cycle.')
                pending = remaining
            return {'events': count, 'replayed': replayed, 'extraction': 'asynchronous',
                    'coverage': 'archived_events_only'}
        except StoreError:
            raise
        except Exception:
            raise StoreError('MEMORY_REBUILD_FAILED', 'Memory reconstruction stopped; retry the same target to resume safely.') from None

    def _replay_event(self, scope, binding, restore, key, apply, previewed):
        document = self._read(key, max_bytes=_MAX_ARCHIVE_BYTES)
        if not isinstance(document, dict) or {k: document.get(k) for k in scope} != scope:
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archived event belongs to another project or source.')
        event = document['event']
        if key != self._event_key(scope, document):
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archived event identity does not match its key.')
        source = event['memoryId'] if event['memoryId'] == 'receipts' else _memory_id(event['memoryId'])
        if source != scope['source_memory_id']:
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archived event changed its source Memory.')
        identity = (event['actorId'], event['sessionId'], event['eventId'])
        checkpoint = restore + _hash(identity) + '.json'
        saved = self._read(checkpoint)
        if saved is not None:
            if saved.get('source_key') != key or saved.get('source_hash') != _hash(document):
                raise StoreError('STORAGE_CONFLICT', 'The reconstruction checkpoint does not match its original.')
            return True, 0
        request = {key: deepcopy(event[key]) for key in ('actorId', 'sessionId', 'payload')}
        timestamp = datetime.fromisoformat(event['eventTimestamp'])
        if _timestamp(timestamp) != event['eventTimestamp']:
            raise StoreError('MEMORY_ARCHIVE_INVALID', 'The archived timestamp is not normalized.')
        request.update(memoryId=binding['target_memory_id'], eventTimestamp=timestamp,
                       clientToken=_hash([binding, document]))
        if 'metadata' in event:
            request['metadata'] = deepcopy(event['metadata'])
        if event.get('branch'):
            branch = deepcopy(event['branch'])
            if branch.get('rootEventId'):
                root_identity = (event['actorId'], event['sessionId'], branch['rootEventId'])
                root = self._read(restore + _hash(root_identity) + '.json')
                if root is None and (apply or root_identity not in previewed):
                    return False, 0
                if root:
                    branch['rootEventId'] = root['event_id']
            request['branch'] = branch
        validate_parameters(request, self.create_shape)
        if apply:
            result = self.memory.create_event(**request)['event']
            if (_memory_id(result['memoryId']), result['actorId'], result['sessionId']) != (
                    binding['target_memory_id'], event['actorId'], event['sessionId']):
                raise StoreError('MEMORY_ARCHIVE_INVALID', 'The reconstructed event changed its project session.')
            self._immutable(checkpoint, {'source_key': key, 'source_hash': _hash(document), 'event_id': result['eventId']})
        else:
            previewed.add(identity)
        return True, int(apply)


def lambda_handler(event, context):
    """Scheduled capture uses deployment-owned configuration, independently of trigger input."""
    import os
    from pydantic import ValidationError
    from daeri.config.memory import MemoryArchiveSettings, build_memory_archive

    try:
        settings = MemoryArchiveSettings.model_validate_json(os.environ.get('DAERI_MEMORY_ARCHIVE_CONFIG', ''))
    except ValidationError:
        raise StoreError('INVALID_INPUT', 'Configure the scheduled project Memory archive before invoking it.') from None
    with build_memory_archive(settings) as archive:
        return archive.capture_batch(settings.project_id, settings.source_memory_id,
                                     catalog=settings.project_parameter_prefix)
