"""A foreign bundle must not cross profile, body, or provenance boundaries."""

import copy
import hashlib
import importlib.util
import io
import json
import sys
import zipfile

import pytest

from daeri.core.errors import StoreError


STAMP = '2026-01-01T00:00:00+00:00'
HASH = 'a' * 64
CONTENT = b'{"request_text":"confirmed service context"}'
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def manifest():
    profile = {'profile_id': 'profile_one', 'name': 'alpha', 'revision': 1,
               'core_context': [], 'resources': [], 'retention_days': None,
               'long_term_memory_days': None, 'memory_policy': None,
               'embedding': {'provider': 'keyword_only'}}
    task = {'task_id': 'task_one', 'profile_id': 'profile_one', 'task_revision': 2,
            'state': 'planning', 'context': {'profile_id': 'profile_one', 'profile_revision': 1,
                                          'profile_snapshot_ref': 'snapshot_one'},
            'profile_snapshot': copy.deepcopy(profile), 'owner_id': None,
            'request_ref': 'record_one', 'context_revision': 1, 'active_plan_ref': None,
            'plans': [], 'approvals': [], 'calls': [], 'last_report_ref': None,
            'usage': {'tool_calls': 0, 'external_requests': 0, 'runtime_ms': 0, 'resource_limits': []},
            'reserved': {'tool_calls': 0, 'external_requests': 0, 'runtime_ms': 0, 'resource_limits': []},
            'waiting_reason': None, 'created_at': STAMP, 'last_activity_at': STAMP}
    return {'format': 'daeri-profile', 'version': 1, 'created_at': STAMP, 'profile': profile,
            'tasks': [{'task_id': 'task_one', 'profile_id': 'profile_one', 'revision': 2,
                       'metadata': task, 'expires_at': None}],
            'records': [{'record_ref': 'record_one', 'task_id': 'task_one', 'kind': 'request',
                         'size': len(CONTENT), 'sha256': DIGEST, 'expires_at': None,
                         'blocked': 0, 'cleaned': 0, 'blob': 'blobs/' + DIGEST}],
            'artifacts': [], 'requests': [], 'memory_heads': [], 'memory_versions': []}


def validate(value, entries=None):
    # An absent entry point is a missing feature, never an import-time collection error.
    assert importlib.util.find_spec('daeri.config.profile_bundle_validation') is not None
    from daeri.config.profile_bundle_validation import validate_manifest
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(value))
        for name, content in (entries if entries is not None else [('blobs/' + DIGEST, CONTENT)]):
            archive.writestr(name, content)
    with zipfile.ZipFile(data) as archive:
        return validate_manifest(value, archive)


def memory(value):
    resource = {'instance_id': 'source', 'type': 'Source', 'adapter': 'source_read',
                'target': {'server': 'https://git.example.test', 'project': 'team/service'},
                'identity_ref': 'identity:reader',
                'connection': {'transport': 'stdio', 'command': sys.executable, 'args': ['-c', 'pass']},
                'capabilities': [{'capability_id': 'read', 'tool_name': 'get_repository_file',
                                  'constraints': {'fixed': {'project_id': 'team/service'}}}]}
    value['profile']['resources'] = [resource]
    value['tasks'][0]['metadata']['profile_snapshot']['resources'] = [copy.deepcopy(resource)]
    target_hash = hashlib.sha256(json.dumps(resource['target'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    source = {'task_id': 'task_one', 'record_ref': 'record_one', 'fields': [{'segments': []}],
              'instance_ids': ['source'], 'target_bindings': {'source': target_hash},
              'record_kind': 'request', 'available': True, 'model_allowed': True,
              'retain_allowed': True, 'long_term_allowed': True, 'export_allowed': True,
              'expires_at': None, 'verified_at': STAMP}
    body = {'category': 'lesson', 'statement': 'Use the confirmed service context.',
            'applicability': 'service operation', 'instance_ids': ['source'],
            'verification_method': 'user confirmation', 'retention': 'long_term',
            '_target_bindings': {'source': target_hash}, '_sources': [source],
            'memory_ref': 'memory_revision', 'verified_at': STAMP, 'expires_at': None}
    value['memory_heads'] = [{'memory_id': 'memory_identity', 'profile_id': 'profile_one',
                              'current_ref': 'memory_revision', 'invalidated': 0}]
    value['memory_versions'] = [{'memory_ref': 'memory_revision', 'memory_id': 'memory_identity',
                                 'profile_id': 'profile_one', 'version': 1, 'body': body,
                                 'content_hash': HASH, 'source_hash': HASH,
                                 'created_at': STAMP, 'expires_at': None,
                                 'source_lineage': {'keys': [hashlib.sha256(
                                     b'["task_one","record_one"]').hexdigest()], 'blocked_at': None}}]
    return value


def test_accepts_owned_graph_without_mutating_source_or_requiring_a_store():
    value = memory(manifest())
    before = copy.deepcopy(value)
    assert validate(value) is None
    assert value == before


@pytest.mark.parametrize('table', ['tasks', 'records', 'memory_heads', 'memory_versions'])
def test_rejects_cross_profile_or_missing_task_ownership(table):
    value = memory(manifest())
    field = 'task_id' if table == 'records' else 'profile_id'
    value[table][0][field] = 'foreign'
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


@pytest.mark.parametrize('table', ['tasks', 'records', 'memory_heads', 'memory_versions', 'requests'])
def test_rejects_duplicate_rows(table):
    value = memory(manifest())
    value['requests'] = [{'scope': 'call:task_one', 'request_key': 'call_one', 'fingerprint': HASH}]
    value[table].append(copy.deepcopy(value[table][0]))
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


@pytest.mark.parametrize('damage', ['missing', 'extra', 'wrong_size', 'wrong_name', 'path'])
def test_rejects_unmanifested_or_mismatched_blobs(damage):
    value = manifest()
    entries = [('blobs/' + DIGEST, CONTENT)]
    if damage == 'missing':
        entries = []
    elif damage == 'extra':
        entries.append(('blobs/' + HASH, b'extra'))
    elif damage == 'wrong_size':
        value['records'][0]['size'] += 1
    elif damage == 'wrong_name':
        value['records'][0]['blob'] = 'blobs/' + HASH
    else:
        value['records'][0]['blob'] = '../outside'
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value, entries)


@pytest.mark.parametrize('damage', ['source', 'kind', 'head', 'current', 'version', 'body_ref', 'expiry', 'lineage', 'binding'])
def test_rejects_dangling_or_inconsistent_memory_provenance(damage):
    value = memory(manifest())
    version = value['memory_versions'][0]
    if damage == 'source':
        version['body']['_sources'][0]['record_ref'] = 'record_missing'
    elif damage == 'kind':
        value['records'][0]['kind'] = 'approval'
    elif damage == 'head':
        version['memory_id'] = 'memory_missing'
    elif damage == 'current':
        value['memory_heads'][0]['current_ref'] = 'memory_missing'
    elif damage == 'version':
        version['version'] = True
    elif damage == 'body_ref':
        version['body']['memory_ref'] = 'memory_missing'
    elif damage == 'expiry':
        version['body']['expires_at'] = STAMP
    elif damage == 'lineage':
        version['source_lineage']['keys'] = ['not-a-digest']
    else:
        version['body']['_target_bindings'] = {}
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


@pytest.mark.parametrize('path,bad', [
    (('version',), True), (('profile', 'revision'), True),
    (('tasks', 0, 'revision'), True), (('records', 0, 'size'), False),
    (('records', 0, 'blocked'), True), (('records', 0, 'cleaned'), '0'),
    (('tasks', 0, 'metadata', 'task_revision'), 3),
    (('tasks', 0, 'metadata', 'calls'), {}),
    (('tasks', 0, 'metadata', 'state'), 'executing-arbitrary-code'),
    (('tasks', 0, 'metadata', 'profile_snapshot', 'profile_id'), 'foreign'),
    (('created_at',), '2026-01-01T00:00:00'),
])
def test_rejects_wrong_scalar_types_and_control_shapes(path, bad):
    value = manifest()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


@pytest.mark.parametrize('table', [None, 'profile', 'tasks', 'records', 'memory_heads', 'memory_versions'])
def test_rejects_unknown_schema_fields_and_filesystem_paths(table):
    value = memory(manifest())
    target = value if table is None else value[table] if table == 'profile' else value[table][0]
    target['path'] = '/private/foreign.json'
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


def test_preserves_metadata_only_blocked_and_expired_history():
    value = memory(manifest())
    value['records'][0].update(blob=None, blocked=1, cleaned=1, expires_at=STAMP)
    value['memory_heads'][0]['invalidated'] = 1
    value['memory_versions'][0].update(body=None, expires_at=STAMP,
                                       source_lineage={'keys': [HASH], 'blocked_at': STAMP})
    assert validate(value, []) is None


def test_old_archive_bytes_may_expire_before_restore():
    value = manifest()
    value['records'][0]['expires_at'] = STAMP
    assert validate(value) is None


def test_known_record_links_are_checked_without_interpreting_free_text():
    value = manifest()
    value['tasks'][0]['metadata']['arbitrary_note'] = 'record_missing'
    assert validate(value) is None
    value['tasks'][0]['metadata']['memory_job_refs'] = ['record_missing']
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


def test_rejects_artifact_whose_record_is_not_an_artifact():
    value = manifest()
    value['artifacts'] = [{'artifact_ref': 'record_one', 'task_id': 'task_one',
                           'extension': 'bin', 'size': len(CONTENT), 'sha256': DIGEST,
                           'blob': 'blobs/' + DIGEST}]
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


@pytest.mark.parametrize('payload', [b'[]', b'{"record_ref":"record_missing"}',
    b'{"_source_paths":{"record_missing":[""]}}', b'{"a":1,"a":2}', b'{"x":NaN}'])
def test_rejects_unsafe_or_dangling_registered_json_bodies(payload):
    value = manifest()
    digest = hashlib.sha256(payload).hexdigest()
    value['records'][0].update(size=len(payload), sha256=digest, blob='blobs/' + digest)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value, [('blobs/' + digest, payload)])


def test_external_result_values_do_not_become_internal_references():
    payload = b'{"result":{"record_ref":"customer-facing-name","plan_id":"customer-plan"}}'
    value = manifest()
    digest = hashlib.sha256(payload).hexdigest()
    value['records'][0].update(size=len(payload), sha256=digest, blob='blobs/' + digest)
    assert validate(value, [('blobs/' + digest, payload)]) is None


@pytest.mark.parametrize('damage', ['active_plan', 'request_kind', 'plan_context', 'snapshot_resources',
                                   'binding_target', 'source_binding', 'approval_state', 'request_scope'])
def test_rejects_invalid_typed_control_and_target_bindings(damage):
    value = memory(manifest())
    task = value['tasks'][0]['metadata']
    if damage == 'active_plan':
        task['active_plan_ref'] = 'plan_without_version'
    elif damage == 'request_kind':
        value['records'][0]['kind'] = 'local_check'
        value['memory_versions'][0]['body']['_sources'][0]['record_kind'] = 'local_check'
    elif damage == 'plan_context':
        task['additional_context'] = {'profile_id': 'profile_foreign'}
    elif damage == 'snapshot_resources':
        task['profile_snapshot']['resources'] = 'invalid resources'
    elif damage == 'binding_target':
        body = value['memory_versions'][0]['body']
        body['_target_bindings']['source'] = HASH
        body['_sources'][0]['target_bindings']['source'] = HASH
    elif damage == 'source_binding':
        value['memory_versions'][0]['body']['_sources'][0]['target_bindings']['source'] = HASH
    elif damage == 'approval_state':
        task['approvals'] = [{'approval_request_id': 'approval_request_one', 'plan_id': 'plan_one',
                             'plan_version': 1, 'state': [], 'decision': None, 'feedback_ref': None}]
    else:
        value['requests'] = [{'scope': 'call:foreign_task', 'request_key': 'one', 'fingerprint': HASH}]
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


def test_rejects_more_than_the_aggregate_row_limit():
    value = manifest()
    value['requests'] = [{'scope': 'daeri_task_create:local', 'request_key': str(index), 'fingerprint': HASH}
                         for index in range(100000)]
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


def test_current_memory_head_cannot_reactivate_a_superseded_version():
    value = memory(manifest())
    later = copy.deepcopy(value['memory_versions'][0])
    later.update(memory_ref='memory_later', version=2, body=None)
    value['memory_versions'].append(later)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


def test_provenance_must_not_reference_itself_even_with_a_present_record():
    value = manifest()
    payload = b'{"evidence_refs":["record_one"]}'
    digest = hashlib.sha256(payload).hexdigest()
    value['records'][0].update(kind='report', size=len(payload), sha256=digest, blob='blobs/' + digest)
    value['tasks'][0]['metadata']['request_ref'] = None
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value, [('blobs/' + digest, payload)])


def test_preserves_caller_chosen_call_ids_accepted_by_the_public_api():
    value = manifest()
    plan_record = copy.deepcopy(value['records'][0])
    plan_record.update(record_ref='record_plan', kind='plan', blob=None)
    value['records'].append(plan_record)
    task = value['tasks'][0]['metadata']
    task['plans'] = [{'plan_id': 'plan_one', 'plan_version': 1,
                      'body_ref': 'record_plan', 'render_ref': 'record_plan'}]
    task['calls'] = [{'call_id': 'user call / 한글', 'plan_id': 'plan_one', 'plan_version': 1,
                     'step_id': 'one', 'call_state': 'completed', 'external_effect': 'none'}]
    assert validate(value) is None


@pytest.mark.parametrize('record_deadline', [None, '2026-01-03T00:00:00+00:00'])
def test_record_deadline_cannot_outlive_its_finite_task_deadline(record_deadline):
    value = manifest()
    value['tasks'][0]['expires_at'] = '2026-01-02T00:00:00+00:00'
    value['records'][0]['expires_at'] = record_deadline
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


@pytest.mark.parametrize('record_deadline', [STAMP, '2026-01-02T09:00:00+09:00'])
def test_record_deadline_may_equal_or_precede_task_deadline(record_deadline):
    value = manifest()
    value['tasks'][0]['expires_at'] = '2026-01-02T00:00:00+00:00'
    value['records'][0]['expires_at'] = record_deadline
    assert validate(value) is None


@pytest.mark.parametrize('source', ['record', 'provenance'])
@pytest.mark.parametrize('memory_deadline', [None, '2026-01-03T00:00:00+00:00'])
def test_source_bound_memory_cannot_outlive_any_finite_source(source, memory_deadline):
    value = memory(manifest())
    version = value['memory_versions'][0]
    version['body']['retention'] = 'source_bound'
    version['expires_at'] = version['body']['expires_at'] = memory_deadline
    if source == 'record':
        value['records'][0]['expires_at'] = '2026-01-02T00:00:00+00:00'
    else:
        version['body']['_sources'][0]['expires_at'] = '2026-01-02T00:00:00+00:00'
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


@pytest.mark.parametrize('retention,deadline', [('source_bound', STAMP), ('long_term', None),
                                             ('long_term', '2026-01-04T00:00:00+00:00')])
def test_memory_respects_shortened_sources_without_rewriting_saved_provenance(retention, deadline):
    value = memory(manifest())
    version = value['memory_versions'][0]
    version['body']['retention'] = retention
    version['expires_at'] = version['body']['expires_at'] = deadline
    value['records'][0]['expires_at'] = '2026-01-02T00:00:00+00:00'
    version['body']['_sources'][0]['expires_at'] = '2026-01-03T00:00:00+00:00'
    assert validate(value) is None


@pytest.mark.parametrize('keys', [None, [], [HASH]])
def test_available_memory_lineage_must_match_its_actual_source_records(keys):
    value = memory(manifest())
    value['memory_versions'][0]['source_lineage']['keys'] = keys
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)


def test_bodyless_memory_keeps_unknown_source_lineage():
    value = memory(manifest())
    value['memory_versions'][0]['body'] = None
    value['memory_versions'][0]['source_lineage']['keys'] = None
    assert validate(value) is None


@pytest.mark.parametrize('field', ['profile_revision', 'task_revision', 'record_size', 'artifact_size', 'memory_version'])
@pytest.mark.parametrize('amount', [2**63, 2**100])
def test_db_integer_fields_must_fit_sqlite_before_restoring(field, amount):
    value = memory(manifest())
    if field == 'profile_revision':
        value['profile']['revision'] = amount
    elif field == 'task_revision':
        value['tasks'][0]['revision'] = value['tasks'][0]['metadata']['task_revision'] = amount
    elif field == 'record_size':
        value['records'][0].update(size=amount, blob=None)
    elif field == 'artifact_size':
        value['records'][0].update(kind='artifact', blob=None)
        value['tasks'][0]['metadata']['request_ref'] = None
        value['memory_heads'] = value['memory_versions'] = []
        value['artifacts'] = [{'artifact_ref': 'record_one', 'task_id': 'task_one', 'extension': 'bin',
                               'size': amount, 'sha256': DIGEST, 'blob': None}]
    else:
        value['memory_versions'][0]['version'] = amount
    entries = [] if field in {'record_size', 'artifact_size'} else None
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value, entries)


def test_bodyless_size_can_preserve_the_largest_existing_sqlite_integer():
    value = manifest()
    value['records'][0].update(size=2**63 - 1, blob=None)
    assert validate(value, []) is None


@pytest.mark.parametrize('missing', ['descriptor', 'file'])
def test_artifact_file_and_descriptor_must_have_the_same_availability(missing):
    value = manifest()
    value['records'][0]['kind'] = 'artifact'
    value['tasks'][0]['metadata']['request_ref'] = None
    value['artifacts'] = [{'artifact_ref': 'record_one', 'task_id': 'task_one',
                           'extension': 'bin', 'size': len(CONTENT), 'sha256': DIGEST,
                           'blob': 'blobs/' + DIGEST}]
    value['records' if missing == 'descriptor' else 'artifacts'][0]['blob'] = None
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate(value)
