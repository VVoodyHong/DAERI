"""One-profile logical snapshots; authenticated validation precedes destination writes."""

import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import tempfile
from datetime import UTC, datetime
from uuid import uuid4

from .memory import MemoryService
from .policy import as_pointer
from .portable_archive import open_bundle, write_bundle
from .profile_bundle_validation import TASK_REQUEST_SCOPES, validate_manifest
from .profile_deletion import _check_busy, track_resources
from .setup import _relocate_managed_resource
from .storage import StoreError, _json, _task_json, _time


_INTERNAL = {'memory_pending', 'memory_done'}
_TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled'}


def _expired(deadline):
    return deadline is not None and datetime.fromisoformat(deadline) <= datetime.now(UTC)


def _summary(manifest):
    profile = manifest['profile']
    return {'profile_id': profile['profile_id'], 'name': profile['name'], 'revision': profile['revision'],
            'created_at': manifest['created_at'],
            'counts': {key: len(manifest[key]) for key in
                       ('tasks', 'records', 'artifacts', 'requests', 'memory_heads', 'memory_versions')},
            'confirmation_token': hashlib.sha256(_json(manifest)).hexdigest()}


def inspect_backup(path, passphrase):
    with open_bundle(path, passphrase) as (manifest, archive):
        validate_manifest(manifest, archive)
        return _summary(manifest)


def _memory_rows(engine, profile):
    identity = profile['profile_id']
    heads = [dict(row) for row in engine.store._db.execute(
        'SELECT * FROM memory_heads WHERE profile_id=? ORDER BY memory_id', (identity,))]
    current = {head['memory_id']: head for head in heads}
    _, policy = engine.memory._policy(identity)
    versions = []
    for raw in engine.store._db.execute('SELECT * FROM memory_versions WHERE profile_id=? ORDER BY memory_id,version', (identity,)):
        row = {key: raw[key] for key in ('memory_ref', 'memory_id', 'profile_id', 'version', 'content_hash',
                                        'source_hash', 'created_at', 'expires_at')}
        body = json.loads(raw['body']) if raw['body'] else None
        lineage = json.loads(raw['source_lineage'])
        head = current[row['memory_id']]
        if (body is not None and not head['invalidated'] and not _expired(row['expires_at'])
                and lineage['blocked_at'] is None and policy.get('exports_allowed') is True
                and engine.memory._allowed(body, policy, profile=profile)
                and engine.memory._resource_allowed(body, profile, export=True)):
            if body['retention'] == 'source_bound':
                deadlines = [row['expires_at']]
                for source in body['_sources']:
                    current_expiry = engine.store._db.execute(
                        'SELECT expires_at FROM records WHERE task_id=? AND record_ref=?',
                        (source['task_id'], source['record_ref'])).fetchone()[0]
                    deadlines.extend((source['expires_at'], current_expiry))
                finite = [datetime.fromisoformat(value) for value in deadlines if value is not None]
                row['expires_at'] = body['expires_at'] = min(finite).isoformat() if finite else None
            row['body'] = body
        else:
            row['body'] = None
        row['source_lineage'] = lineage
        versions.append(row)
    return heads, versions


def _record_payload(engine, task, row):
    if row['blocked'] or row['cleaned'] or _expired(row['expires_at']) or row['kind'] in _INTERNAL:
        return None
    payload = engine.store.read_record(task['task_id'], row['record_ref'])['payload']
    kind = row['kind']
    if kind == 'call_result' and 'intent' not in payload and payload.get('result') is not None:
        view = engine._load_evidence(task, row['record_ref'], mode='export', require_current_plan=False)
        # Plans, worker prose and opaque files may repeat any observed value. V1 requires a full
        # exportable view rather than claiming that projecting the raw result also sanitizes prose.
        observed = {as_pointer(tuple(path)) for path in payload.get('model_paths', [])}
        if view['payload']['result'] != payload['result'] or not observed <= set(view['allowed_paths']):
            raise StoreError('ACCESS_DENIED', 'The complete task history requires export permission for its retained fields.')
    if kind == 'plan':
        for reference in payload.get('draft', {}).get('memory_refs', []):
            if not engine.memory.can_export(task['profile_id'], reference):
                raise StoreError('ACCESS_DENIED', 'The complete plan requires export permission for its selected memories.')
    if kind in {'report', 'table', 'artifact', 'worker_result'}:
        engine._load_evidence(task, row['record_ref'], mode='export', require_current_plan=False)
    if kind in {'memory_search', 'memory_update'}:
        for reference in payload.get('memory_refs', []):
            if not engine.memory.can_export(task['profile_id'], reference):
                raise StoreError('ACCESS_DENIED', 'Recalled memories require current export permission for the complete task history.')
        # Search/update records are regenerated for the destination's current context.
        return None
    if kind == 'artifact':
        payload.pop('_workspace_source', None)
    return payload


def _snapshot(engine, profile, folder):
    store, identity = engine.store, profile['profile_id']
    _check_busy(store, identity)
    heads, memories = _memory_rows(engine, profile)
    manifest = {'format': 'daeri-profile', 'version': 1, 'created_at': datetime.now(UTC).isoformat(),
                'profile': profile, 'tasks': [], 'records': [], 'artifacts': [], 'requests': [],
                'memory_heads': heads, 'memory_versions': memories}
    tasks = {}
    for raw in store._db.execute('SELECT * FROM tasks WHERE profile_id=? ORDER BY task_id', (identity,)):
        task = json.loads(raw['metadata'])
        tasks[task['task_id']] = task
        manifest['tasks'].append({key: raw[key] for key in ('task_id', 'profile_id', 'revision', 'expires_at')} | {'metadata': task})
    blobs = {}
    for raw in store._db.execute('''SELECT r.* FROM records r JOIN tasks t USING(task_id)
                                   WHERE t.profile_id=? ORDER BY r.record_ref''', (identity,)):
        row = {key: raw[key] for key in ('record_ref', 'task_id', 'kind', 'size', 'sha256', 'expires_at', 'blocked', 'cleaned')}
        payload = _record_payload(engine, tasks[row['task_id']], row)
        row['blob'] = None
        if payload is not None:
            content = _json(payload)
            row['size'], row['sha256'] = len(content), hashlib.sha256(content).hexdigest()
            row['blob'] = 'blobs/' + row['sha256']
            target = folder / row['sha256']
            target.write_bytes(content)
            target.chmod(0o600)
            blobs[row['blob']] = target
        manifest['records'].append(row)
    records = {row['record_ref']: row for row in manifest['records']}
    for raw in store._db.execute('''SELECT a.* FROM artifact_files a JOIN tasks t USING(task_id)
                                   WHERE t.profile_id=? ORDER BY a.artifact_ref''', (identity,)):
        row = {key: raw[key] for key in ('artifact_ref', 'task_id', 'size', 'sha256')}
        row['extension'] = Path(raw['path']).suffix[1:]
        row['blob'] = None
        if records[row['artifact_ref']]['blob'] is not None:
            verified = store.artifact_file(row['task_id'], row['artifact_ref'])
            row['blob'] = 'blobs/' + row['sha256']
            target = folder / row['sha256']
            with Path(verified['local_path']).open('rb') as incoming, target.open('wb') as outgoing:
                shutil.copyfileobj(incoming, outgoing, 128 * 1024)
            target.chmod(0o600)
            blobs[row['blob']] = target
        manifest['artifacts'].append(row)
    owned = {tuple(json.loads(raw[0])) for raw in store._db.execute(
        "SELECT reference FROM profile_references WHERE profile_id=? AND kind='request'", (identity,))}
    foreign = {tuple(json.loads(raw[0])) for raw in store._db.execute(
        "SELECT reference FROM profile_references WHERE profile_id<>? AND kind='request'", (identity,))}
    for raw in store._db.execute('SELECT * FROM requests ORDER BY scope,request_key'):
        key = raw['scope'], raw['request_key']
        if key in foreign and key not in owned:
            continue
        cached = json.loads(raw['response'])
        cached_task = cached.get('task_id') if isinstance(cached, dict) else None
        if isinstance(cached, dict) and isinstance(cached.get('data'), dict):
            cached_task = cached['data'].get('task_id', cached_task)
        namespace, _, subject = raw['scope'].partition(':')
        inferred = (namespace in TASK_REQUEST_SCOPES and subject in tasks or
                    raw['scope'] == 'daeri_task_create:local' and cached_task in tasks)
        if key in owned or inferred:
            manifest['requests'].append({key: raw[key] for key in ('scope', 'request_key', 'fingerprint')})
    # Runtime grants are never portable, including hashed tokens and process owner identities.
    for row in manifest['tasks']:
        _revoke_runtime(row['metadata'])
    return manifest, blobs


def backup_profile(engine, name_or_id, destination, passphrase):
    # Copy registered bytes under the publication lock; encrypt only the completed snapshot.
    with tempfile.TemporaryDirectory(prefix='daeri-profile-') as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        with engine.store.transaction():
            profile = engine.store.get_profile(name_or_id)
            manifest, blobs = _snapshot(engine, profile, folder)
        # Validate the exact writer's graph before it becomes an externally visible bundle.
        from zipfile import ZipFile
        with tempfile.TemporaryFile() as stream:
            with ZipFile(stream, 'w') as archive:
                archive.writestr('manifest.json', _json(manifest))
                for name, path in blobs.items():
                    archive.write(path, name)
            stream.seek(0)
            with ZipFile(stream) as archive:
                validate_manifest(manifest, archive)
        write_bundle(Path(destination), manifest, blobs, passphrase)
        return {**_summary(manifest), 'path': str(Path(destination).absolute())}


def _revoke_runtime(task):
    task['owner_id'] = None
    task.pop('paused_waiting', None)
    task['memory_job_refs'] = []
    for approval in task['approvals']:
        approval.setdefault('historical_state', approval['state'])
        approval.setdefault('historical_ever_approved', approval.get('ever_approved', False) or approval['state'] == 'approved')
        approval['ever_approved'] = False
        if approval['state'] in {'approved', 'awaiting_response'}:
            approval['state'] = 'invalidated'
    for call in task['calls']:
        call.pop('execution_owner_id', None)
    for grant in task.get('delegations', []):
        grant['token_hash'] = ''
        grant['revoked'] = True
        grant['owner_id'] = None
        grant['state'] = 'revoked'
    if task['state'] not in _TERMINAL:
        task['state'] = 'paused'
        task['waiting_reason'] = {'kind': 'pause', 'message': '복원한 업무의 소유권과 새 승인을 확인하세요.'}


def _fresh_connections(store, profile, mapping):
    def fresh(reference):
        if reference not in mapping:
            mapping[reference] = 'credential:restore.' + uuid4().hex
        return mapping[reference]
    for resource in profile['resources']:
        _relocate_managed_resource(store, resource)
        for field in ('auth_env', 'auth_headers'):
            for key, reference in resource['connection'].get(field, {}).items():
                resource['connection'][field][key] = fresh(reference)
    embedding = profile['embedding']
    if embedding.get('credential_ref'):
        embedding['credential_ref'] = fresh(embedding['credential_ref'])


def _collisions(store, manifest):
    identity, name = manifest['profile']['profile_id'], manifest['profile']['name']
    store.assert_profile_available(identity)
    if store._db.execute('SELECT 1 FROM profiles WHERE profile_id=? OR name=?', (identity, name)).fetchone():
        raise StoreError('PROFILE_ALREADY_EXISTS', 'Restore requires an unused profile and original identifiers.')
    fields = {'tasks': ('task_id',), 'records': ('record_ref',), 'memory_heads': ('memory_id',),
              'memory_versions': ('memory_ref',), 'requests': ('scope', 'request_key')}
    for table, keys in fields.items():
        for row in manifest[table]:
            where = ' AND '.join(key + '=?' for key in keys)
            if store._db.execute(f'SELECT 1 FROM {table} WHERE {where}', tuple(row[key] for key in keys)).fetchone():
                raise StoreError('REVISION_CONFLICT', 'Restore requires unused original identifiers.')
    # Record/memory IDs share the public evidence-reference namespace.
    for table, key, rows, source_key in (
        ('records', 'record_ref', manifest['memory_versions'], 'memory_ref'),
        ('memory_versions', 'memory_ref', manifest['records'], 'record_ref')):
        for row in rows:
            if store._db.execute(f'SELECT 1 FROM {table} WHERE {key}=?', (row[source_key],)).fetchone():
                raise StoreError('REVISION_CONFLICT', 'Restore requires unused evidence identifiers.')


def _publish(store, folder, archive, row, extension, published):
    if folder == 'records':
        path, size, digest = store._prepare_bytes(folder, archive.read(row['blob']), extension)
        published.append(path)
        return path, size, digest
    token = uuid4().hex
    staging = store._path(f'staging/{token}.json', 'staging')
    reference = f'{folder}/{token}.{extension}'
    target = store._path(reference, folder)
    try:
        with os.fdopen(os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as outgoing:
            with archive.open(row['blob']) as incoming:
                shutil.copyfileobj(incoming, outgoing, 128 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        staging.rename(target)
        published.append(reference)
        if os.name != 'nt':
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError:
        staging.unlink(missing_ok=True)
        raise StoreError('STORE_UNAVAILABLE', 'A restored file could not be published.') from None
    return reference, row['size'], row['sha256']


def restore_profile(store, path, passphrase, confirmation):
    with open_bundle(path, passphrase) as (original, archive):
        validate_manifest(original, archive)
        preview = _summary(original)
        if not isinstance(confirmation, str) or not hmac.compare_digest(confirmation, preview['confirmation_token']):
            raise StoreError('REVISION_CONFLICT', 'Inspect the current backup preview before restoring.')
        manifest = copy.deepcopy(original)
        # Storage uses canonical UTC strings for indexed deadline comparisons.
        for table in ('tasks', 'records', 'memory_versions'):
            for row in manifest[table]:
                if row['expires_at'] is not None:
                    row['expires_at'] = _time(row['expires_at'])
                if table == 'memory_versions':
                    row['created_at'] = _time(row['created_at'])
                    if row['body'] is not None:
                        row['body']['expires_at'] = row['expires_at']
        published, mapping = [], {}
        # Schema creation is idempotent; all profile rows, files and FTS publishing share one commit.
        service = MemoryService(store, auto_load=False, cleanup=False)
        identity = manifest['profile']['profile_id']
        try:
            with store.transaction():
                _collisions(store, manifest)
                _fresh_connections(store, manifest['profile'], mapping)
                profile = manifest['profile']
                profile_path, size, digest = store._prepare('profiles', profile)
                published.append(profile_path)
                store._db.execute('INSERT INTO profiles VALUES(?,?,?,?,?,?)',
                                  (identity, profile['name'], profile['revision'], profile_path, size, digest))
                store.track_profile_reference(identity, 'file', profile_path)
                track_resources(store, profile)
                unavailable = {row['record_ref'] for row in manifest['records'] if
                               row['blob'] is None or row['blocked'] or row['cleaned'] or _expired(row['expires_at'])}
                unavailable.update(row['artifact_ref'] for row in manifest['artifacts'] if row['blob'] is None)
                for row in manifest['tasks']:
                    task = row['metadata']
                    _revoke_runtime(task)
                    _fresh_connections(store, task['profile_snapshot'], mapping)
                    active = task['active_plan_ref']
                    if active and any(plan['plan_id'] == active['plan_id'] and
                                      (plan['body_ref'] in unavailable or plan['render_ref'] in unavailable) for plan in task['plans']):
                        task['active_plan_ref'] = None
                    store._db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)',
                                      (row['task_id'], identity, row['revision'], _task_json(task), row['expires_at']))
                    track_resources(store, task['profile_snapshot'])
                for row in manifest['records']:
                    absent = row['record_ref'] in unavailable
                    if absent:
                        prepared = (f'records/{uuid4().hex}.json', row['size'], row['sha256'])
                    else:
                        prepared = _publish(store, 'records', archive, row, 'json', published)
                    store._db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',
                                      (row['record_ref'], row['task_id'], row['kind'], *prepared, row['expires_at'],
                                       1 if absent else row['blocked'], 1 if absent else row['cleaned']))
                    if not absent:
                        store.track_profile_reference(identity, 'file', prepared[0])
                for row in manifest['artifacts']:
                    absent = row['artifact_ref'] in unavailable or row['blob'] is None
                    if absent:
                        prepared = (f"artifacts/{uuid4().hex}.{row['extension']}", row['size'], row['sha256'])
                        store._db.execute('UPDATE records SET blocked=1,cleaned=1 WHERE record_ref=?', (row['artifact_ref'],))
                    else:
                        prepared = _publish(store, 'artifacts', archive, row, row['extension'], published)
                    store._db.execute('INSERT INTO artifact_files VALUES(?,?,?,?,?)', (row['artifact_ref'], row['task_id'], *prepared))
                    if not absent:
                        store.track_profile_reference(identity, 'file', prepared[0])
                for row in manifest['memory_heads']:
                    store._db.execute('INSERT INTO memory_heads VALUES(?,?,?,?)',
                                      tuple(row[key] for key in ('memory_id', 'profile_id', 'current_ref', 'invalidated')))
                invalid = {row['memory_id'] for row in manifest['memory_heads'] if row['invalidated']}
                for row in manifest['memory_versions']:
                    body = row['body']
                    if _expired(row['expires_at']) or row['memory_id'] in invalid or row['source_lineage']['blocked_at']:
                        body = None
                    store._db.execute('''INSERT INTO memory_versions(memory_ref,memory_id,profile_id,version,body,
                        content_hash,source_hash,created_at,expires_at,index_state,source_lineage) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (row['memory_ref'], row['memory_id'], identity, row['version'], _json(body).decode() if body else None,
                         row['content_hash'], row['source_hash'], row['created_at'], row['expires_at'], 'pending',
                         _json(row['source_lineage']).decode()))
                for row in manifest['requests']:
                    store.request_put(row['scope'], row['request_key'], row['fingerprint'],
                                      {'restored_request': True}, profile_id=identity)
                indexes = service.reindex(identity, vectors=False)
        except BaseException:
            # Remove only newly published, provably unregistered paths after rollback. If commit
            # succeeded but reporting failed, its registered files remain owned by that commit.
            with store.transaction():
                for reference in published:
                    exists = store._db.execute('''SELECT 1 FROM profiles WHERE path=? UNION SELECT 1 FROM records WHERE path=?
                        UNION SELECT 1 FROM artifact_files WHERE path=?''', (reference,) * 3).fetchone()
                    if not exists:
                        try:
                            store._path(reference, reference.split('/')[0]).unlink(missing_ok=True)
                        except OSError:
                            pass
            raise
        return {**preview, 'restored': True, 'credential_references': sorted(mapping.values()), 'index': indexes}
