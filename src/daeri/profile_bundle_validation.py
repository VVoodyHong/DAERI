"""Validate a logical profile archive before publishing any destination state."""

import hashlib
import json
import math
import re
from collections import deque
from datetime import datetime
from zipfile import BadZipFile

from .embedding import validate_embedding_config
from .memory import _source_key
from .policy import validate_resource_policy, validate_selectors
from .resources import validate_resource
from .setup import _no_secrets, _pairs
from .storage import StoreError, _profile_memory_policy, retention_deadline


_ROWS = {
    'tasks': 'task_id profile_id revision metadata expires_at',
    'records': 'record_ref task_id kind size sha256 expires_at blocked cleaned blob',
    'artifacts': 'artifact_ref task_id extension size sha256 blob',
    'requests': 'scope request_key fingerprint',
    'memory_heads': 'memory_id profile_id current_ref invalidated',
    'memory_versions': 'memory_ref memory_id profile_id version body content_hash source_hash created_at expires_at source_lineage',
}
_PROFILE = set('profile_id name revision core_context resources retention_days long_term_memory_days memory_policy embedding'.split())
_STATES = {'planning', 'awaiting_input', 'awaiting_approval', 'running', 'reconciling',
           'verifying', 'paused', 'succeeded', 'partial', 'failed', 'cancelled'}
_KINDS = {'request', 'clarification', 'local_check', 'call_result', 'plan', 'approval',
          'worker_assignment', 'worker_result', 'report', 'table', 'artifact',
          'memory_search', 'memory_update', 'memory_pending', 'memory_done'}
_SINGLE = {'request_ref', 'record_ref', 'body_ref', 'render_ref', 'feedback_ref', 'intent_ref',
           'result_ref', 'last_report_ref', 'assignment_ref', 'report_ref', 'artifact_ref',
           'clarification_ref', 'memory_ref', 'local_check_ref'}
_MULTIPLE = {'record_refs', 'source_refs', 'evidence_refs', 'artifact_refs', 'memory_refs',
             'memory_search_refs', 'memory_job_refs', 'carried_evidence_refs'}
_OPAQUE = {'result', 'intent', 'input_schema', 'output_schema', 'scope', 'constraints',
           'fixed', 'parameters', 'target', 'connection', 'core_context', 'desired_output'}
_MESSAGE = 'The profile archive has an invalid schema or inconsistent references.'
_SQLITE_MAX = 2**63 - 1
TASK_REQUEST_SCOPES = {'call', 'worker', 'daeri_task_update', 'daeri_task_resume', 'daeri_task_cancel',
                       'daeri_plan_submit', 'daeri_approval_request', 'daeri_result_submit', 'daeri_memory_update'}


def _require(condition):
    if not condition:
        raise StoreError('INVALID_INPUT', _MESSAGE)


def _object(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields.split() if isinstance(fields, str) else fields))


def _text(value, limit=4096):
    _require(isinstance(value, str) and 0 < len(value) <= limit and '\x00' not in value)
    value.encode('utf-8')


def _identifier(value):
    _require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', value))


def _integer(value, minimum=0, *, maximum=None):
    _require(type(value) is int and value >= minimum and (maximum is None or value <= maximum))


def _flag(value):
    _require(type(value) is int and value in (0, 1))


def _digest(value):
    _require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value))


def _instant(value, *, nullable=False):
    if value is None and nullable:
        return
    _text(value, 128)
    parsed = datetime.fromisoformat(value)
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None)


def _json_tree(value, depth=0):
    _require(depth <= 32)
    if isinstance(value, dict):
        _require(all(isinstance(key, str) for key in value))
        for key, item in value.items():
            key.encode('utf-8')
            _json_tree(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_tree(item, depth + 1)
    else:
        _require(value is None or type(value) in (str, int, float, bool))
        _require(type(value) is not float or math.isfinite(value))
        if isinstance(value, str):
            value.encode('utf-8')


def _profile(value):
    _object(value, _PROFILE)
    _identifier(value['profile_id'])
    _require(isinstance(value['name'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value['name']))
    _integer(value['revision'], 1, maximum=_SQLITE_MAX)
    _require(isinstance(value['core_context'], list) and all(isinstance(item, str) for item in value['core_context']))
    _require(isinstance(value['resources'], list))
    instances = []
    for resource in value['resources']:
        validate_resource(resource)
        validate_resource_policy(resource.get('policy'))
        instances.append(resource['instance_id'])
    _require(len(set(instances)) == len(instances))
    retention_deadline(value['retention_days'])
    retention_deadline(value['long_term_memory_days'])
    _profile_memory_policy(value['memory_policy'], value['resources'])
    _require(isinstance(value['embedding'], dict))
    validate_embedding_config(value['embedding'])
    _no_secrets(value)


def _budget(value):
    _object(value, 'tool_calls external_requests runtime_ms resource_limits')
    for key in ('tool_calls', 'external_requests', 'runtime_ms'):
        if value[key] is not None:
            _integer(value[key])
    _require(isinstance(value['resource_limits'], list) and all(isinstance(item, dict) for item in value['resource_limits']))


def _indexed(rows, field):
    result = {}
    for row in rows:
        if field == 'call_id':
            _text(row[field], 65536)
            _require(bool(row[field].strip()))
        else:
            _identifier(row[field])
        _require(row[field] not in result)
        result[row[field]] = row
    return result


def _task(row, identity):
    _require(row['profile_id'] == identity)
    _integer(row['revision'], maximum=_SQLITE_MAX)
    _instant(row['expires_at'], nullable=True)
    task = row['metadata']
    required = set('task_id profile_id task_revision state context profile_snapshot owner_id request_ref context_revision active_plan_ref plans approvals calls last_report_ref usage reserved waiting_reason created_at last_activity_at'.split())
    _require(isinstance(task, dict) and required <= task.keys())
    _require(task['task_id'] == row['task_id'] and task['profile_id'] == identity)
    _integer(task['task_revision'])
    _require(task['task_revision'] == row['revision'] and isinstance(task['state'], str) and task['state'] in _STATES)
    _integer(task['context_revision'], 1)
    _require(task['owner_id'] is None or isinstance(task['owner_id'], str))
    _require(isinstance(task['context'], dict) and task['context'].get('profile_id') == identity)
    _integer(task['context'].get('profile_revision'), 1)
    snapshot = task['profile_snapshot']
    _require(isinstance(snapshot, dict) and snapshot.get('profile_id') == identity)
    _require(not set(snapshot) - _PROFILE)
    _profile({'core_context': [], **snapshot})
    for field in ('plans', 'approvals', 'calls', 'delegations'):
        if field in task:
            _require(isinstance(task[field], list) and all(isinstance(item, dict) for item in task[field]))
    for field in ('usage', 'reserved'):
        _budget(task[field])
    _require(task['waiting_reason'] is None or isinstance(task['waiting_reason'], dict))
    for field in ('created_at', 'last_activity_at', 'finished_at'):
        if field in task:
            _instant(task[field])
    _indexed(task['plans'], 'plan_id')
    _indexed(task['calls'], 'call_id')
    _indexed(task['approvals'], 'approval_request_id')
    _indexed(task.get('delegations', []), 'delegation_id')
    for plan in task['plans']:
        _require({'plan_id', 'plan_version', 'body_ref', 'render_ref'} <= plan.keys())
        _integer(plan['plan_version'], 1)
    for approval in task['approvals']:
        _require({'approval_request_id', 'plan_id', 'plan_version', 'state', 'decision', 'feedback_ref'} <= approval.keys())
        _require(approval['state'] in {'awaiting_response', 'approved', 'invalidated', 'rejected', 'cancelled'})
        _integer(approval['plan_version'], 1)
    for call in task['calls']:
        _require({'call_id', 'plan_id', 'plan_version', 'step_id', 'call_state', 'external_effect'} <= call.keys())
        _require(call['call_state'] in {'pending', 'unknown', 'blocked', 'failed', 'completed'})
        _require(call['external_effect'] in {'none', 'unknown', 'confirmed', 'not_applied', 'applied'})
        _integer(call['plan_version'], 1)
        for field in ('runtime_ms', 'reserved_runtime_ms'):
            if field in call:
                _integer(call[field])
        if 'dispatched' in call:
            _require(type(call['dispatched']) is bool)


def _links(value, task_id, records, memories, tasks, *, plans, linked=None):
    linked = set() if linked is None else linked

    def reference(ref):
        _identifier(ref)
        _require(ref in memories or ref in records and records[ref]['task_id'] == task_id)
        linked.add(ref)

    if isinstance(value, list):
        for item in value:
            _links(item, task_id, records, memories, tasks, plans=plans, linked=linked)
    elif isinstance(value, dict):
        if 'profile_id' in value:
            _require(value['profile_id'] == tasks[task_id]['profile_id'])
        if 'plan_id' in value:
            _require(value['plan_id'] in plans)
            if 'plan_version' in value:
                _integer(value['plan_version'], 1)
                _require(value['plan_version'] == plans[value['plan_id']]['plan_version'])
        for key, item in value.items():
            if key in _OPAQUE:
                continue
            if key in {'active_plan_ref', 'source_plan_ref', 'plan_ref'}:
                if item is not None:
                    _object(item, 'plan_id plan_version')
                    _links(item, task_id, records, memories, tasks, plans=plans, linked=linked)
            elif key in _SINGLE:
                if item is not None:
                    reference(item)
                    kinds = {'request_ref': {'request'}, 'body_ref': {'plan'}, 'render_ref': {'plan'},
                             'feedback_ref': {'approval'}, 'intent_ref': {'call_result'},
                             'result_ref': {'call_result', 'worker_result'}, 'last_report_ref': {'report'},
                             'report_ref': {'report'}, 'assignment_ref': {'worker_assignment'},
                             'artifact_ref': {'artifact'}, 'clarification_ref': {'clarification'},
                             'local_check_ref': {'local_check'}}
                    if key in kinds:
                        _require(item in records and records[item]['kind'] in kinds[key])
                    elif key == 'memory_ref':
                        _require(item in memories)
            elif key in _MULTIPLE:
                _require(isinstance(item, list))
                _require(len(set(item)) == len(item))
                for ref in item:
                    reference(ref)
            elif key in {'_source_paths', 'artifact_permissions', 'clarification_times'}:
                _require(isinstance(item, dict))
                for ref, proof in item.items():
                    reference(ref)
                    if key == 'artifact_permissions':
                        _require(ref in records and records[ref]['kind'] == 'artifact')
                        _links(proof, task_id, records, memories, tasks, plans=plans, linked=linked)
                    elif key == 'clarification_times':
                        _instant(proof)
                    else:
                        _require(isinstance(proof, list) and all(isinstance(path, str) for path in proof))
            elif key == 'external_call_refs':
                _require(isinstance(item, list))
                for call in item:
                    _object(call, 'task_id call_id')
                    _require(call['task_id'] in tasks and any(
                        prior['call_id'] == call['call_id'] for prior in tasks[call['task_id']]['metadata']['calls']))
            else:
                _links(item, task_id, records, memories, tasks, plans=plans, linked=linked)


def _acyclic(edges):
    remaining = {reference: len(sources) for reference, sources in edges.items()}
    dependents = {reference: [] for reference in edges}
    for reference, sources in edges.items():
        for source in sources:
            dependents[source].append(reference)
    ready = deque(reference for reference, count in remaining.items() if count == 0)
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for reference in dependents[source]:
            remaining[reference] -= 1
            if remaining[reference] == 0:
                ready.append(reference)
    _require(visited == len(edges))


def _memory_body(row, records, resources):
    body = row['body']
    if body is None:
        return
    _object(body, 'category statement applicability instance_ids verification_method retention _target_bindings _sources memory_ref verified_at expires_at')
    _require(body['memory_ref'] == row['memory_ref'] and body['expires_at'] == row['expires_at'])
    _require(body['category'] in {'service_rule', 'resource_structure', 'procedure', 'lesson'})
    _require(body['retention'] in {'source_bound', 'long_term'})
    for field in ('statement', 'applicability', 'verification_method'):
        _text(body[field], 16384)
    _instant(body['verified_at'])
    instances = body['instance_ids']
    _require(isinstance(instances, list) and instances and all(isinstance(item, str) for item in instances))
    _require(len(set(instances)) == len(instances) and set(instances) <= resources.keys())
    bindings = body['_target_bindings']
    _require(isinstance(bindings, dict) and set(bindings) == set(instances))
    for digest in bindings.values():
        _digest(digest)
    for instance, digest in bindings.items():
        target = json.dumps(resources[instance]['target'], sort_keys=True, ensure_ascii=False,
                            allow_nan=False, separators=(',', ':')).encode('utf-8')
        _require(digest == hashlib.sha256(target).hexdigest())
    sources = body['_sources']
    _require(isinstance(sources, list) and 0 < len(sources) <= 100)
    contributing, deadlines = set(), []
    for source in sources:
        required = set('task_id record_ref fields instance_ids target_bindings record_kind export_allowed expires_at verified_at'.split())
        allowed = required | {'available', 'model_allowed', 'retain_allowed', 'long_term_allowed'}
        _require(isinstance(source, dict) and required <= source.keys() and set(source) <= allowed)
        ref = source['record_ref']
        _require(ref in records and source['task_id'] == records[ref]['task_id'])
        _require(source['record_kind'] == records[ref]['kind'] and source['record_kind'] in {'request', 'clarification', 'local_check', 'call_result'})
        validate_selectors(source['fields'])
        _require(bool(source['fields']))
        selected = source['instance_ids']
        _require(isinstance(selected, list) and selected and all(isinstance(item, str) for item in selected))
        _require(len(set(selected)) == len(selected) and set(selected) <= set(instances))
        _require(isinstance(source['target_bindings'], dict) and set(source['target_bindings']) == set(selected))
        _require(all(source['target_bindings'][key] == bindings[key] for key in selected))
        contributing.update(selected)
        _instant(source['expires_at'], nullable=True)
        deadlines.extend(datetime.fromisoformat(deadline) for deadline in
                         (source['expires_at'], records[ref]['expires_at']) if deadline is not None)
        _instant(source['verified_at'])
        for field in ('export_allowed', 'available', 'model_allowed', 'retain_allowed', 'long_term_allowed'):
            if field in source:
                _require(type(source[field]) is bool)
    _require(contributing == set(instances))
    if body['retention'] == 'source_bound' and deadlines:
        _require(row['expires_at'] is not None and datetime.fromisoformat(row['expires_at']) <= min(deadlines))
    keys = row['source_lineage']['keys']
    _require(keys is not None and set(keys) == {_source_key(source) for source in sources})


def validate_manifest(value, archive):
    """Reject malformed or cross-profile graphs without opening a local store."""
    try:
        _validate_manifest(value, archive)
    except (StoreError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
            RecursionError, UnicodeError, OSError, BadZipFile):
        raise StoreError('INVALID_INPUT', _MESSAGE) from None


def _validate_manifest(value, archive):
    _object(value, {'format', 'version', 'created_at', 'profile', *_ROWS})
    _require(value['format'] == 'daeri-profile' and type(value['version']) is int and value['version'] == 1)
    _instant(value['created_at'])
    _json_tree(value)
    _profile(value['profile'])
    identity = value['profile']['profile_id']
    for table, fields in _ROWS.items():
        _require(isinstance(value[table], list))
    _require(sum(len(value[table]) for table in _ROWS) <= 100000)
    for table, fields in _ROWS.items():
        for row in value[table]:
            _object(row, fields)
    tasks = _indexed(value['tasks'], 'task_id')
    records = _indexed(value['records'], 'record_ref')
    artifacts = _indexed(value['artifacts'], 'artifact_ref')
    heads = _indexed(value['memory_heads'], 'memory_id')
    memories = _indexed(value['memory_versions'], 'memory_ref')
    _require(not set(records) & set(memories))
    for row in tasks.values():
        _task(row, identity)
    plans = {}
    for task_id, row in tasks.items():
        plans[task_id] = _indexed(row['metadata']['plans'], 'plan_id')
    blobs = set()
    for row in [*records.values(), *artifacts.values()]:
        _require(row['task_id'] in tasks)
        _integer(row['size'], maximum=_SQLITE_MAX)
        _digest(row['sha256'])
        blob = row['blob']
        if blob is not None:
            _require(blob == 'blobs/' + row['sha256'])
            _require(archive.getinfo(blob).file_size == row['size'])
            blobs.add(blob)
    for row in records.values():
        _require(isinstance(row['kind'], str) and row['kind'] in _KINDS)
        _instant(row['expires_at'], nullable=True)
        task_deadline = tasks[row['task_id']]['expires_at']
        if task_deadline is not None:
            _require(row['expires_at'] is not None
                     and datetime.fromisoformat(row['expires_at']) <= datetime.fromisoformat(task_deadline))
        _flag(row['blocked'])
        _flag(row['cleaned'])
    for row in artifacts.values():
        _require(row['artifact_ref'] in records)
        record = records[row['artifact_ref']]
        _require(record['kind'] == 'artifact' and record['task_id'] == row['task_id'])
        _require((record['blob'] is None) == (row['blob'] is None))
        _require(isinstance(row['extension'], str) and re.fullmatch('[a-z0-9]{1,8}', row['extension']))
    _require(all(row['record_ref'] in artifacts for row in records.values() if row['kind'] == 'artifact'))
    request_keys = set()
    for row in value['requests']:
        _text(row['scope'])
        _text(row['request_key'])
        _digest(row['fingerprint'])
        key = row['scope'], row['request_key']
        _require(key not in request_keys)
        request_keys.add(key)
        if ':' in row['scope'] and row['scope'].partition(':')[0] in TASK_REQUEST_SCOPES:
            _require(row['scope'].partition(':')[2] in tasks)
    for row in heads.values():
        _require(row['profile_id'] == identity and row['current_ref'] in memories)
        _require(memories[row['current_ref']]['memory_id'] == row['memory_id'])
        _flag(row['invalidated'])
    versions = set()
    latest = {}
    resources = {item['instance_id']: item for item in value['profile']['resources']}
    for row in memories.values():
        _require(row['profile_id'] == identity and row['memory_id'] in heads)
        _integer(row['version'], 1, maximum=_SQLITE_MAX)
        key = row['memory_id'], row['version']
        _require(key not in versions)
        versions.add(key)
        latest[row['memory_id']] = max(latest.get(row['memory_id'], 0), row['version'])
        _digest(row['content_hash'])
        _digest(row['source_hash'])
        _instant(row['created_at'])
        _instant(row['expires_at'], nullable=True)
        lineage = row['source_lineage']
        _object(lineage, 'keys blocked_at')
        _require(lineage['keys'] is None or isinstance(lineage['keys'], list))
        if lineage['keys'] is not None:
            for digest in lineage['keys']:
                _digest(digest)
            _require(len(set(lineage['keys'])) == len(lineage['keys']))
        _instant(lineage['blocked_at'], nullable=True)
        _memory_body(row, records, resources)
    _require(all(memories[head['current_ref']]['version'] == latest[head['memory_id']]
                 for head in heads.values()))
    for task_id, row in tasks.items():
        _links(row['metadata'], task_id, records, memories, tasks, plans=plans[task_id])
    names = archive.namelist()
    _require(len(names) == len(set(names)) and set(names) == {'manifest.json', *blobs})
    edges = {reference: set() for reference in [*records, *memories]}
    for row in memories.values():
        if row['body'] is not None:
            edges[row['memory_ref']].update(source['record_ref'] for source in row['body']['_sources'])
    for row in records.values():
        if row['blob'] is None:
            continue
        _require(row['size'] <= 32 * 1024 * 1024)
        payload = json.loads(archive.read(row['blob']).decode('utf-8'), object_pairs_hook=_pairs,
                             parse_constant=lambda _: _require(False))
        _require(isinstance(payload, dict))
        _json_tree(payload)
        _links(payload, row['task_id'], records, memories, tasks, plans=plans[row['task_id']],
               linked=edges[row['record_ref']])
    _acyclic(edges)
