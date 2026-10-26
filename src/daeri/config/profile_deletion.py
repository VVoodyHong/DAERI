"""Local profile cascade, resumable after filesystem or native-store failures."""

import hashlib
import hmac
import json
import re
import shutil

from daeri.core.errors import StoreError
from daeri.infrastructure.storage import process_alive


def resource_references(profile):
    from daeri.infrastructure.adapters import get_adapter
    from daeri.infrastructure.resources import _hash
    references = {('directory', 'setup/' + profile['name']),
                  ('file', 'config/profiles/' + profile['name'] + '.yaml')}
    embedding_ref = profile.get('embedding', {}).get('credential_ref', '')
    if embedding_ref.startswith('credential:'):
        references.add(('credential', embedding_ref))
    for resource in profile.get('resources', []):
        references.add(('file', 'resources/' + _hash([profile['profile_id'], resource['instance_id']]) + '.json'))
        connection = resource['connection']
        for ref in (*connection.get('auth_env', {}).values(), *connection.get('auth_headers', {}).values()):
            if ref.startswith('credential:'):
                references.add(('credential', ref))
        if connection['transport'] == 'stdio':
            try:
                launched = get_adapter(resource['adapter']).launch_connection(resource)
            except StoreError:
                continue  # A connection that cannot launch has no new process workspace.
            references.add(('directory', 'resources/processes/' + _hash(launched)))
    return references


def track_resources(store, profile):
    for kind, reference in resource_references(profile):
        store.track_profile_reference(profile['profile_id'], kind, reference)


def _lookup(store, name_or_id):
    rows = store._db.execute('SELECT * FROM profiles WHERE name=? OR profile_id=?', (name_or_id, name_or_id)).fetchall()
    if len(rows) != 1:
        raise StoreError('NOT_FOUND' if not rows else 'CONTEXT_REQUIRED', 'Select one existing profile for deletion.')
    return rows[0]


def _token(profile_id, revision):
    return hashlib.sha256(json.dumps([profile_id, revision], separators=(',', ':')).encode()).hexdigest()


def _references(store, profile, tasks):
    refs = {tuple(row) for row in store._db.execute(
        "SELECT kind,reference FROM profile_references WHERE profile_id=? AND kind<>'operation'", (profile['profile_id'],))}
    refs.add(('file', profile['path']))
    config = store._read(profile, 'STORE_UNAVAILABLE')
    refs.update(resource_references(config))
    for task in tasks:
        snapshot = task.get('profile_snapshot')
        if snapshot:
            refs.update(resource_references(snapshot))
    return refs


def _manifest(store, row):
    identity = row['profile_id']
    tasks = [json.loads(r[0]) for r in store._db.execute('SELECT metadata FROM tasks WHERE profile_id=?', (identity,))]
    refs = _references(store, row, tasks)
    files = store._db.execute('''SELECT path FROM records WHERE task_id IN(SELECT task_id FROM tasks WHERE profile_id=?)
        UNION SELECT path FROM artifact_files WHERE task_id IN(SELECT task_id FROM tasks WHERE profile_id=?)''',
        (identity, identity)).fetchall()
    refs.update(('file', r[0]) for r in files)
    other_refs = {tuple(r) for r in store._db.execute(
        "SELECT kind,reference FROM profile_references WHERE profile_id<>? AND kind<>'operation'", (identity,))}
    # Older stores did not retain ownership; inspect their current configurations and task snapshots too.
    for other in store._db.execute('SELECT * FROM profiles WHERE profile_id<>?', (identity,)):
        pending = store._db.execute("SELECT manifest FROM profile_deletions WHERE profile_id=? AND state='deleting'",
                                    (other['profile_id'],)).fetchone()
        if pending:
            other_refs.update(tuple(ref) for ref in json.loads(pending[0])['references'])
        else:
            other_refs.update(resource_references(store._read(other, 'STORE_UNAVAILABLE')))
    for other in store._db.execute('SELECT metadata FROM tasks WHERE profile_id<>?', (identity,)):
        snapshot = json.loads(other[0]).get('profile_snapshot')
        if snapshot:
            other_refs.update(resource_references(snapshot))
    tables = {r[0] for r in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    memories = store._db.execute('SELECT count(*) FROM memory_versions WHERE profile_id=?', (identity,)).fetchone()[0] if 'memory_versions' in tables else 0
    return {'profile_id': identity, 'name': row['name'], 'revision': row['revision'],
            'references': [list(r) for r in sorted(refs)],
            'shared': [list(r) for r in sorted(refs & other_refs)],
            'counts': {'tasks': len(tasks), 'records': store._db.execute(
                'SELECT count(*) FROM records WHERE task_id IN(SELECT task_id FROM tasks WHERE profile_id=?)', (identity,)).fetchone()[0],
                'artifacts': store._db.execute('SELECT count(*) FROM artifact_files WHERE task_id IN(SELECT task_id FROM tasks WHERE profile_id=?)', (identity,)).fetchone()[0],
                'memory_versions': memories,
                'credentials': sum(kind == 'credential' for kind, ref in refs - other_refs),
                'shared_credentials': sum(kind == 'credential' for kind, ref in refs & other_refs)}}


def _public(manifest, state):
    result = {key: manifest[key] for key in ('profile_id', 'name', 'revision', 'counts')} | {
        'state': state, 'deletion_token': _token(manifest['profile_id'], manifest['revision'])}
    if 'last_error' in manifest:
        result['last_error'] = manifest['last_error']
    return result


def preview(store, name_or_id):
    with store.transaction():
        row = _lookup(store, name_or_id)
        job = store._db.execute('SELECT * FROM profile_deletions WHERE profile_id=?', (row['profile_id'],)).fetchone()
        return _public(json.loads(job['manifest']) if job else _manifest(store, row), 'deleting' if job else 'ready')


def _check_busy(store, identity):
    for row in store._db.execute('SELECT metadata FROM tasks WHERE profile_id=?', (identity,)):
        if any(call.get('call_state') == 'pending' for call in json.loads(row[0]).get('calls', [])):
            raise StoreError('PROFILE_BUSY', 'Wait for the profile resource call to finish before deleting it.')
    for row in store._db.execute("SELECT reference FROM profile_references WHERE profile_id=? AND kind='operation'", (identity,)).fetchall():
        try:
            alive = process_alive(int(row[0].split(':', 1)[0]))
        except (ValueError, OverflowError, OSError):
            raise StoreError('PROFILE_BUSY', 'The profile operation owner must be checked before deletion.') from None
        if alive:
            raise StoreError('PROFILE_BUSY', 'Wait for profile setup or diagnosis to finish before deleting it.')
        store._db.execute("DELETE FROM profile_references WHERE profile_id=? AND kind='operation' AND reference=?", (identity, row[0]))


def _managed_path(store, reference, directory=False):
    pattern = (r'(?:resources/processes|workspaces)/[0-9a-f]{64}|setup/[A-Za-z0-9][A-Za-z0-9_-]{0,63}' if directory else
               r'(?:profiles|records)/[0-9a-f]{32}\.json|artifacts/[0-9a-f]{32}\.[a-z0-9]{1,8}|resources/[0-9a-f]{64}\.json|config/profiles/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.yaml')
    if not isinstance(reference, str) or not re.fullmatch(pattern, reference):
        raise StoreError('STORE_UNAVAILABLE', 'The deletion manifest contains an unmanaged path.')
    path = store.data_dir / reference
    managed = [store.data_dir]
    for token in reference.split('/'):
        managed.append(managed[-1] / token)
    if any(parent.is_symlink() for parent in managed):
        raise StoreError('STORE_UNAVAILABLE', 'Managed deletion paths cannot be symbolic links.')
    return path


def delete(store, name_or_id, confirmation, credentials):
    with store.transaction():
        row = _lookup(store, name_or_id)
        identity = row['profile_id']
        job = store._db.execute('SELECT * FROM profile_deletions WHERE profile_id=?', (identity,)).fetchone()
        manifest = json.loads(job['manifest']) if job else _manifest(store, row)
        if (not isinstance(confirmation, str) or not re.fullmatch('[0-9a-f]{64}', confirmation)
                or not hmac.compare_digest(confirmation, _token(identity, manifest['revision']))):
            raise StoreError('REVISION_CONFLICT', 'Inspect the current profile deletion preview before confirming.')
        # Existing cleanup jobs can predate the user configuration path.
        config_reference = ['file', 'config/profiles/' + row['name'] + '.yaml']
        if job and config_reference not in manifest['references']:
            manifest['references'].append(config_reference)
            store._db.execute('UPDATE profile_deletions SET manifest=? WHERE profile_id=?',
                              (json.dumps(manifest), identity))
        if not job:
            _check_busy(store, identity)
            for kind, reference in manifest['references']:
                store.track_profile_reference(identity, kind, reference)
            store._db.execute("INSERT INTO profile_deletions VALUES(?,'deleting',?)", (identity, json.dumps(manifest)))
    stage = 'files'
    try:
        with store.transaction():
            job = store._db.execute('SELECT state FROM profile_deletions WHERE profile_id=?', (identity,)).fetchone()
            if job[0] == 'deleted':
                return {'profile_id': identity, 'deleted': True}
            shared = {tuple(ref) for ref in manifest['shared']}
            owned = {tuple(ref) for ref in manifest['references']} - shared
            for kind, reference in sorted(owned):
                if kind == 'credential':
                    stage = 'credentials'
                    credentials.delete_if_present(reference)
                elif kind == 'file' and not reference.startswith('profiles/'):
                    stage = 'files'
                    _managed_path(store, reference).unlink(missing_ok=True)
                elif kind == 'directory':
                    stage = 'files'
                    path = _managed_path(store, reference, directory=True)
                    if path.exists():
                        shutil.rmtree(path)
            from daeri.infrastructure.memory import purge_profile_memory
            stage = 'memory'
            purge_profile_memory(store, identity)
            stage = 'metadata'
            for kind, reference in owned:
                if kind == 'request':
                    scope, key = json.loads(reference)
                    store._db.execute('DELETE FROM requests WHERE scope=? AND request_key=?', (scope, key))
            store._db.execute('''DELETE FROM requests WHERE substr(scope,instr(scope,':')+1)
                IN(SELECT task_id FROM tasks WHERE profile_id=?) OR json_extract(response,'$.data.task_id')
                IN(SELECT task_id FROM tasks WHERE profile_id=?)''', (identity, identity))
            store._db.execute('DELETE FROM artifact_files WHERE task_id IN(SELECT task_id FROM tasks WHERE profile_id=?)', (identity,))
            store._db.execute('DELETE FROM records WHERE task_id IN(SELECT task_id FROM tasks WHERE profile_id=?)', (identity,))
            store._db.execute('DELETE FROM tasks WHERE profile_id=?', (identity,))
            for kind, reference in owned:
                if kind == 'file' and reference.startswith('profiles/'):
                    stage = 'files'
                    _managed_path(store, reference).unlink(missing_ok=True)
            stage = 'metadata'
            store._db.execute('DELETE FROM profile_references WHERE profile_id=?', (identity,))
            # Only the opaque deleted ID remains, so late work cannot recreate this profile's data.
            store._db.execute("UPDATE profile_deletions SET state='deleted',manifest=NULL WHERE profile_id=?", (identity,))
            store._db.execute('DELETE FROM profiles WHERE profile_id=?', (identity,))
        return {'profile_id': identity, 'deleted': True, 'counts': manifest['counts']}
    except (OSError, StoreError) as error:
        manifest['last_error'] = {'stage': stage, 'code': getattr(error, 'code', 'LOCAL_FILE_ERROR')}
        with store.transaction():
            store._db.execute("UPDATE profile_deletions SET manifest=? WHERE profile_id=? AND state='deleting'",
                             (json.dumps(manifest), identity))
        raise StoreError('PROFILE_DELETE_PENDING', 'Profile use is blocked. Resolve the cleanup failure and retry its deletion.') from error
