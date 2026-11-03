"""S3 task manifests and immutable turn receipts, with conditional writes."""

import base64
from dataclasses import asdict
from hashlib import sha256
import json
import re
from uuid import UUID, uuid4

from daeri.core.approval import PlanApproval
from daeri.core.cloud import AccessContext, CloudTask, CloudTurn, HarnessReply
from daeri.core.errors import StoreError
from daeri.core.serialization import validate_storable_json


_MAX_BYTES = 8 * 1024 * 1024
_REJECTED_INVOCATIONS = {
    'HARNESS_ACCESS_DENIED', 'HARNESS_NOT_FOUND', 'HARNESS_THROTTLED',
    'HARNESS_VALIDATION_ERROR', 'HARNESS_QUOTA_EXCEEDED',
}


def normalize_storage_prefix(prefix: str) -> str:
    normalized = prefix[:-1] if isinstance(prefix, str) and prefix.endswith('/') else prefix
    if (not isinstance(normalized, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', normalized)
            or any(part in {'.', '..'} for part in normalized.split('/')) or len(normalized) > 787):
        # Reserve 236 bytes for two hashed scopes, task/turn UUIDs, and path separators.
        raise StoreError('INVALID_INPUT', 'Supply a relative S3 prefix within 788 ASCII bytes, without empty or dot segments.')
    return normalized + '/'


def validate_storage_bucket(bucket: str) -> str:
    if (not isinstance(bucket, str) or not 3 <= len(bucket) <= 63
            or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*[a-z0-9]', bucket)
            or any(part in bucket for part in ('..', '.-', '-.'))
            or re.fullmatch(r'\d+\.\d+\.\d+\.\d+', bucket)
            or bucket.startswith(('xn--', 'sthree-', 'amzn_s3_demo_'))
            or bucket.endswith(('-s3alias', '--ol-s3', '.mrap', '--x-s3', '--table-s3'))):
        raise StoreError('INVALID_INPUT', 'Supply a general-purpose S3 bucket name.')
    return bucket


def _uuid(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise StoreError('INVALID_INPUT', 'Supply a valid task or turn UUID.') from None


def _encode(value):
    data = validate_storable_json(value)
    if len(data) > _MAX_BYTES:
        raise ValueError('Oversized document')
    return data


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def _error_code(error):
    response = getattr(error, 'response', None)
    details = response.get('Error') if isinstance(response, dict) else None
    code = details.get('Code') if isinstance(details, dict) else None
    return code if isinstance(code, str) else None


def _release_route(endpoint, version):
    if (endpoint == '' and version == '') or (
            isinstance(endpoint, str) and endpoint != 'DEFAULT'
            and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', endpoint)
            and isinstance(version, str) and re.fullmatch(r'[1-9][0-9]{0,4}', version)):
        return
    raise ValueError('Invalid versioned harness route')


def _reply(value):
    if (not isinstance(value, dict)
            or set(value) != {'text', 'stop_reason', 'pending_tools', 'observations', 'error'}
            or not isinstance(value['text'], str) or not isinstance(value['stop_reason'], str)
            or not value['stop_reason'] or not isinstance(value['pending_tools'], list)
            or not isinstance(value['observations'], list)
            or any(not isinstance(item, dict) for item in value['observations'])
            or (value['error'] is not None and (not isinstance(value['error'], str) or not value['error']))):
        raise ValueError('Invalid saved response')
    ids = [item.get('toolUseId') if isinstance(item, dict) else None for item in value['pending_tools']]
    if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid saved pending calls')
    return HarnessReply(value['text'], value['stop_reason'], tuple(value['pending_tools']),
                        tuple(value['observations']), value['error'])


def _approval(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'tool_use_id', 'plan', 'decision', 'response'}
            or not all(isinstance(item, str) for item in value.values())
            or not value['tool_use_id'] or not value['plan'].strip()
            or value['decision'] not in {'pending', 'approve', 'revise', 'reject'}):
        raise ValueError('Invalid approval record')
    return PlanApproval(**value)


def _pending_plan(tools):
    if len(tools) != 1:
        return None
    tool = tools[0]
    data = tool.get('input')
    if (tool.get('name') != 'confirm_plan' or tool.get('type', 'tool_use') != 'tool_use'
            or not isinstance(data, dict) or set(data) != {'plan'}
            or not isinstance(data['plan'], str) or not data['plan'].strip()):
        return None
    return PlanApproval(tool['toolUseId'], data['plan'])


class S3CloudStore:
    """A manifest owns execution; an immutable receipt permits recovery without replay."""

    def __init__(self, client, bucket, prefix='daeri/'):
        self.client, self.bucket, self.prefix = client, validate_storage_bucket(bucket), normalize_storage_prefix(prefix)

    def _root(self, context, task_id):
        try:
            if not isinstance(context, AccessContext) or not all(
                isinstance(item, str) and item.strip() for item in (context.actor_id, context.project_id)
            ):
                raise ValueError
            actor, project = (sha256(item.encode('utf-8')).hexdigest()
                              for item in (context.actor_id, context.project_id))
        except (TypeError, ValueError):
            raise StoreError('INVALID_INPUT', 'Current identity and project must be valid UTF-8.') from None
        root = f'{self.prefix}projects/{project}/actors/{actor}/tasks/{_uuid(task_id)}/'
        if len((root + 'turns/' + '0' * 36 + '.json').encode()) > 1024:
            raise StoreError('INVALID_INPUT', 'The configured storage prefix is too long.')
        return root

    def _get(self, key, missing='STORAGE_UNAVAILABLE'):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            code = missing if _error_code(error) in {'NoSuchKey', 'NotFound', '404'} else 'STORAGE_UNAVAILABLE'
            raise StoreError(code, 'The saved task or turn could not be read.') from None
        body = response.get('Body')
        try:
            if response.get('ContentLength', 0) > _MAX_BYTES:
                raise ValueError('Oversized document')
            data = body.read(_MAX_BYTES + 1)
            if not isinstance(data, bytes) or len(data) > _MAX_BYTES:
                raise ValueError('Invalid document body')
            document = json.loads(data.decode('utf-8'), object_pairs_hook=_unique)
            _encode(document)
            etag = response['ETag']
            if not isinstance(etag, str) or not etag:
                raise ValueError('Missing ETag')
            return document, etag
        except Exception:
            raise StoreError('STORAGE_UNAVAILABLE', 'The saved JSON document is invalid or unavailable.') from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass

    def _put(self, key, document, **condition):
        try:
            data = _encode(document)
        except (TypeError, ValueError, RecursionError):
            raise StoreError('INVALID_INPUT', 'The JSON document must be finite UTF-8 data within 8 MiB.') from None
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                ContentType='application/json', **condition)
            etag = response['ETag']
            if not isinstance(etag, str) or not etag:
                raise ValueError('Missing ETag')
            return etag
        except Exception as error:
            code = 'STORAGE_CONFLICT' if _error_code(error) in {'PreconditionFailed', 'ConditionalRequestConflict'} else 'STORAGE_UNAVAILABLE'
            raise StoreError(code, 'The saved task changed or the storage write could not be confirmed.') from None

    def _manifest(self, context, task_id):
        task_id = _uuid(task_id)
        root = self._root(context, task_id)
        value, etag = self._get(root + 'manifest.json', 'ACCESS_DENIED')
        if not isinstance(value, dict):
            raise StoreError('STORAGE_UNAVAILABLE', 'The saved task manifest is invalid.')
        if (value.get('id'), value.get('actor_id'), value.get('project_id')) != (
            task_id, context.actor_id, context.project_id
        ):
            raise StoreError('ACCESS_DENIED', 'The task is unavailable in the current project and identity.')
        try:
            required = {'id', 'session_id', 'actor_id', 'project_id', 'harness_arn', 'goal',
                        'pending_tools', 'state', 'in_flight', 'last_turn'}
            if (not required <= set(value) or set(value) - required - {
                    'approval', 'harness_endpoint', 'harness_version'}
                    or not isinstance(value['goal'], str) or not isinstance(value['harness_arn'], str)
                    or not value['harness_arn'].strip() or value['state'] not in {'active', 'waiting', 'interrupted'}):
                raise ValueError
            value.setdefault('approval', None)
            value.setdefault('harness_endpoint', '')
            value.setdefault('harness_version', '')
            _release_route(value['harness_endpoint'], value['harness_version'])
            approval = _approval(value['approval'])
            _reply({'text': '', 'stop_reason': 'saved', 'pending_tools': value['pending_tools'],
                    'observations': [], 'error': None})
            pending = _pending_plan(value['pending_tools'])
            if approval is not None and (pending is None or (approval.tool_use_id, approval.plan) != (
                    pending.tool_use_id, pending.plan)):
                raise ValueError
            _uuid(value['session_id'])
            flight = value['in_flight']
            if flight is not None:
                if (not isinstance(flight, dict) or set(flight) != {'turn_id', 'prompt', 'tool_results'}
                        or not isinstance(flight['prompt'], str) or not isinstance(flight['tool_results'], list)
                        or any(not isinstance(item, dict) for item in flight['tool_results'])):
                    raise ValueError
                _uuid(flight['turn_id'])
            last = value['last_turn']
            if last is not None and (not isinstance(last, str) or not last.startswith(root + 'turns/')
                    or not last.endswith('.json') or last != root + 'turns/' + _uuid(last.rsplit('/', 1)[-1][:-5]) + '.json'):
                raise ValueError
        except (KeyError, TypeError, ValueError, StoreError):
            raise StoreError('STORAGE_UNAVAILABLE', 'The saved task manifest is invalid.') from None
        return value, etag

    @staticmethod
    def _task(value, etag):
        return CloudTask(value['id'], value['session_id'],
                         AccessContext(value['actor_id'], value['project_id']),
                         tuple(value['pending_tools']), value['goal'], value['harness_arn'], etag,
                         _approval(value['approval']), 'running' if value['in_flight'] is not None else value['state'],
                         value['harness_endpoint'], value['harness_version'])

    def _session_key(self, harness_arn, session_id):
        if not isinstance(harness_arn, str) or not harness_arn.strip():
            raise StoreError('INVALID_INPUT', 'A saved harness binding is required.')
        route = sha256(harness_arn.encode('utf-8')).hexdigest()
        return f'{self.prefix}sessions/{route}/{_uuid(session_id)}.json'

    def _ensure_session(self, task):
        key = self._session_key(task.harness_arn, task.session_id)
        binding = {'harness_arn': task.harness_arn, 'session_id': task.session_id,
                   'task_id': task.id, 'actor_id': task.context.actor_id, 'project_id': task.context.project_id}
        try:
            self._put(key, binding, IfNoneMatch='*')
        except StoreError as error:
            if error.code != 'STORAGE_CONFLICT':
                raise
            existing, _ = self._get(key)
            if existing != binding:
                raise StoreError('STORAGE_CONFLICT', 'The session is already bound to a different task.') from None

    def hook_get(self, harness_arn, session_id):
        binding, _ = self._get(self._session_key(harness_arn, session_id))
        if (not isinstance(binding, dict)
                or set(binding) != {'harness_arn', 'session_id', 'task_id', 'actor_id', 'project_id'}
                or binding['harness_arn'] != harness_arn or binding['session_id'] != session_id):
            raise StoreError('ACCESS_DENIED', 'The hook session does not match a saved task.')
        value, etag = self._manifest(AccessContext(binding['actor_id'], binding['project_id']), binding['task_id'])
        if (value['harness_arn'] != harness_arn or value['session_id'] != session_id
                or value['in_flight'] is None):
            raise StoreError('ACCESS_DENIED', 'The hook has no matching active invocation.')
        return self._task(value, etag)

    def revoke_approval(self, task):
        value, etag = self._manifest(task.context, task.id)
        if (etag != task.etag or value['in_flight'] is None
                or value['session_id'] != task.session_id or value['harness_arn'] != task.harness_arn):
            raise StoreError('STORAGE_CONFLICT', 'The active task changed before approval revocation.')
        if value['approval'] is not None:
            self._put(self._root(task.context, task.id) + 'manifest.json',
                      {**value, 'approval': None}, IfMatch=etag)

    def create_task(self, context, harness_arn, goal, *, harness_endpoint='', harness_version=''):
        if not isinstance(harness_arn, str) or not harness_arn.strip() or not isinstance(goal, str) or not goal.strip():
            raise StoreError('INVALID_INPUT', 'A configured harness and initial goal are required.')
        try:
            _release_route(harness_endpoint, harness_version)
        except ValueError:
            raise StoreError('INVALID_INPUT', 'Supply a named release endpoint and matching version.') from None
        task_id, session_id = str(uuid4()), str(uuid4())
        root = self._root(context, task_id)
        value = {'id': task_id, 'session_id': session_id, 'actor_id': context.actor_id,
                 'project_id': context.project_id, 'harness_arn': harness_arn, 'goal': goal,
                 'pending_tools': [], 'state': 'active', 'in_flight': None, 'last_turn': None, 'approval': None,
                 'harness_endpoint': harness_endpoint, 'harness_version': harness_version}
        try:
            etag = self._put(root + 'manifest.json', value, IfNoneMatch='*')
            task = self._task(value, etag)
            self._ensure_session(task)
        except StoreError as error:
            error.task_id = task_id
            raise
        return task

    def get_task(self, context, task_id):
        value, etag = self._manifest(context, task_id)
        return self._task(value, etag)

    def list_tasks(self, context, cursor=None, limit=20):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise StoreError('INVALID_INPUT', 'Supply a task page size from 1 through 100.')
        prefix = self._root(context, '00000000-0000-0000-0000-000000000000').rsplit('/', 2)[0] + '/'
        request = {'Bucket': self.bucket, 'Prefix': prefix, 'Delimiter': '/', 'MaxKeys': limit}
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not cursor or len(cursor) > 8192:
                    raise ValueError
                decoded = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
                page = json.loads(decoded.decode('utf-8'), object_pairs_hook=_unique)
                if (not isinstance(page, dict) or set(page) != {'prefix', 'limit', 'token'}
                        or page['prefix'] != prefix or type(page['limit']) is not int or page['limit'] != limit
                        or not isinstance(page['token'], str) or not page['token']):
                    raise ValueError
                request['ContinuationToken'] = page['token']
            except (ValueError, TypeError, UnicodeError):
                raise StoreError('INVALID_INPUT', 'The task cursor does not match this page request.') from None
        try:
            response = self.client.list_objects_v2(**request)
        except Exception as error:
            code = 'INVALID_INPUT' if _error_code(error) in {'InvalidArgument', 'InvalidToken'} else 'STORAGE_UNAVAILABLE'
            raise StoreError(code, 'The task page could not be read.') from None
        try:
            items = []
            for item in response.get('CommonPrefixes', []):
                key = item['Prefix']
                if not isinstance(key, str) or not key.startswith(prefix) or not key.endswith('/'):
                    raise ValueError
                try:
                    task_id = _uuid(key[len(prefix):-1])
                except StoreError:
                    raise ValueError from None
                if key != prefix + task_id + '/':
                    raise ValueError
                items.append(self.get_task(context, task_id))
            next_cursor = None
            if response.get('IsTruncated'):
                token = response['NextContinuationToken']
                if not isinstance(token, str) or not token:
                    raise ValueError
                data = _encode({'prefix': prefix, 'limit': limit, 'token': token})
                next_cursor = base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')
                if len(next_cursor) > 8192:
                    raise ValueError
            return {'items': items, 'next_cursor': next_cursor}
        except (KeyError, ValueError, TypeError):
            raise StoreError('STORAGE_UNAVAILABLE', 'The task page is invalid.') from None

    def record_decision(self, task, tool_use_id, plan, decision, response):
        if (not isinstance(decision, str) or decision not in {'approve', 'revise', 'reject'}
                or not isinstance(response, str)):
            raise StoreError('INVALID_INPUT', 'Supply a supported user decision and actual response.')
        value, etag = self._manifest(task.context, task.id)
        approval = _approval(value['approval'])
        if (etag != task.etag or value['in_flight'] is not None or approval is None
                or (approval.decision != 'pending' and decision != 'revise')
                or (approval.tool_use_id, approval.plan) != (tool_use_id, plan)
                or value['session_id'] != task.session_id or value['harness_arn'] != task.harness_arn):
            raise StoreError('STORAGE_CONFLICT', 'The current waiting plan no longer matches this response.')
        value['approval'] = asdict(PlanApproval(tool_use_id, plan, decision, response))
        etag = self._put(self._root(task.context, task.id) + 'manifest.json', value, IfMatch=etag)
        return self._task(value, etag)

    def begin_turn(self, task, prompt, tool_results):
        value, etag = self._manifest(task.context, task.id)
        if (value['in_flight'] is not None or etag != task.etag
                or value['session_id'] != task.session_id or value['harness_arn'] != task.harness_arn
                or value['harness_endpoint'] != task.harness_endpoint or value['harness_version'] != task.harness_version
                or _encode(value['pending_tools']) != _encode(task.pending_tools)):
            raise StoreError('TASK_BUSY', 'The task changed or already has an execution in progress.')
        if (not isinstance(prompt, str) or not isinstance(tool_results, (list, tuple))
                or any(not isinstance(item, dict) for item in tool_results)):
            raise StoreError('INVALID_INPUT', 'Supply a text request and actual pending tool responses.')
        expected = {item['toolUseId'] for item in value['pending_tools']}
        actual = [item.get('toolUseId') for item in tool_results]
        if any(not isinstance(item, str) for item in actual) or len(set(actual)) != len(actual) or set(actual) != expected:
            raise StoreError('INVALID_INPUT', 'Actual responses must match the current pending tool calls.')
        self._ensure_session(task)
        turn_id = str(uuid4())
        value['in_flight'] = {'turn_id': turn_id, 'prompt': prompt, 'tool_results': tool_results}
        if not tool_results:
            value['approval'] = None
        try:
            self._put(self._root(task.context, task.id) + 'manifest.json', value, IfMatch=task.etag)
        except StoreError as error:
            if error.code == 'STORAGE_CONFLICT':
                raise StoreError('TASK_BUSY', 'The task changed before execution could begin.', task_id=task.id) from None
            error.task_id = task.id
            if error.code == 'STORAGE_UNAVAILABLE':
                error.turn_id = turn_id
            raise
        return turn_id

    def _receipt(self, value, turn_id, *, current_execution=True):
        key = self._root(AccessContext(value['actor_id'], value['project_id']), value['id']) + 'turns/' + _uuid(turn_id) + '.json'
        receipt, _ = self._get(key, 'EXECUTION_UNCERTAIN')
        expected = {'task_id': value['id'], 'turn_id': _uuid(turn_id), 'session_id': value['session_id'],
                    'actor_id': value['actor_id'], 'project_id': value['project_id'], 'harness_arn': value['harness_arn']}
        if value['harness_endpoint'] or value['harness_version']:
            expected.update(harness_endpoint=value['harness_endpoint'], harness_version=value['harness_version'])
        if (not isinstance(receipt, dict) or any(receipt.get(name) != item for name, item in expected.items())
                or receipt.get('harness_endpoint', '') != value['harness_endpoint']
                or receipt.get('harness_version', '') != value['harness_version']):
            raise StoreError('STORAGE_CONFLICT', 'The saved receipt does not belong to this task execution.')
        try:
            allowed = {*expected, 'prompt', 'tool_results', 'reply'}
            if not value['harness_endpoint'] and not value['harness_version']:
                allowed.update({'harness_endpoint', 'harness_version'} & set(receipt))
            if (set(receipt) != allowed
                    or not isinstance(receipt['prompt'], str) or not isinstance(receipt['tool_results'], list)
                    or any(not isinstance(item, dict) for item in receipt['tool_results'])):
                raise ValueError
            reply = _reply(receipt['reply'])
        except (KeyError, TypeError, ValueError):
            raise StoreError('STORAGE_UNAVAILABLE', 'The saved receipt is invalid.') from None
        flight = value['in_flight'] if current_execution else None
        if flight is not None and (flight['turn_id'] != turn_id or receipt['prompt'] != flight['prompt']
                or _encode(receipt['tool_results']) != _encode(flight['tool_results'])):
            raise StoreError('STORAGE_CONFLICT', 'The saved receipt does not match the pending execution.')
        return key, reply

    def _finalize(self, context, value, etag, key, reply):
        state = 'interrupted' if reply.error else 'waiting' if reply.pending_tools else 'active'
        if value['in_flight'] is None:
            if (value['last_turn'] != key or value['state'] != state
                    or _encode(value['pending_tools']) != _encode(reply.pending_tools)):
                raise StoreError('STORAGE_CONFLICT', 'The task no longer matches the saved receipt.')
            return
        approval = None
        pending = _pending_plan(reply.pending_tools)
        if pending is not None and (reply.error is None or reply.error in _REJECTED_INVOCATIONS):
            previous = _approval(value['approval'])
            approval = previous if previous is not None and (
                previous.tool_use_id, previous.plan) == (pending.tool_use_id, pending.plan) else pending
        complete = {**value, 'pending_tools': reply.pending_tools, 'state': state,
                    'in_flight': None, 'last_turn': key,
                    'approval': asdict(approval) if approval is not None else None}
        self._put(self._root(context, value['id']) + 'manifest.json', complete, IfMatch=etag)

    def finish_turn(self, task, turn_id, prompt, reply, tool_results):
        turn_id = _uuid(turn_id)
        value, etag = self._manifest(task.context, task.id)
        key = self._root(task.context, task.id) + 'turns/' + turn_id + '.json'
        flight = value['in_flight']
        if (value['session_id'] != task.session_id or value['harness_arn'] != task.harness_arn
                or value['harness_endpoint'] != task.harness_endpoint or value['harness_version'] != task.harness_version
                or (flight is None and value['last_turn'] != key)
                or (flight is not None and (flight['turn_id'] != turn_id or flight['prompt'] != prompt
                    or _encode(flight['tool_results']) != _encode(tool_results)))):
            raise StoreError('STORAGE_CONFLICT', 'The task no longer owns this execution.')
        receipt = {'task_id': task.id, 'turn_id': turn_id, 'session_id': task.session_id,
                   'actor_id': task.context.actor_id, 'project_id': task.context.project_id,
                   'harness_arn': task.harness_arn, 'harness_endpoint': task.harness_endpoint,
                   'harness_version': task.harness_version, 'prompt': prompt,
                   'tool_results': tool_results, 'reply': asdict(reply)}
        try:
            self._put(key, receipt, IfNoneMatch='*')
        except StoreError as error:
            if error.code != 'STORAGE_CONFLICT':
                raise
            stored, _ = self._get(key)
            if _encode(stored) != _encode(receipt):
                raise StoreError('STORAGE_CONFLICT', 'The immutable turn receipt already has different content.') from None
        _, saved_reply = self._receipt(value, turn_id)
        self._finalize(task.context, value, etag, key, saved_reply)

    def recover_turn(self, context, task_id):
        value, etag = self._manifest(context, task_id)
        flight = value['in_flight']
        if flight is None and value['last_turn'] is None:
            raise StoreError('EXECUTION_UNCERTAIN', 'There is no saved execution receipt to recover.')
        turn_id = flight['turn_id'] if flight is not None else value['last_turn'].rsplit('/', 1)[-1][:-5]
        key, reply = self._receipt(value, turn_id)
        self._finalize(context, value, etag, key, reply)
        return CloudTurn(value['id'], turn_id, reply)

    def read_turn(self, context, task_id):
        """Read the last committed result while leaving any current execution untouched."""
        value, _ = self._manifest(context, task_id)
        if value['last_turn'] is None:
            return None
        turn_id = value['last_turn'].rsplit('/', 1)[-1][:-5]
        _, reply = self._receipt(value, turn_id, current_execution=False)
        return CloudTurn(value['id'], turn_id, reply)
