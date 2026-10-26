"""Profile deletion removes owned data last, isolates other profiles, and resumes safely."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

import daeri.config.setup as setup
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'private')
    yield value
    value.close()


class FakeCredentials:
    def __init__(self):
        self.values = {'credential:private': 'PRIVATE', 'credential:old': 'OLD', 'credential:shared': 'SHARED'}

    def delete_if_present(self, ref):
        return self.values.pop(ref, None) is not None


def resource(instance, credential):
    return {'instance_id': instance, 'type': 'Document', 'adapter': 'confluence_read',
            'target': {'server': 'https://docs.example.invalid/wiki', 'project': 'team/service'},
            'identity_ref': 'identity:reader', 'connection': {'transport': 'stdio',
            'command': sys.executable, 'args': [], 'env': {'CONFLUENCE_URL': 'https://docs.example.invalid/wiki'},
            'auth_env': {'CONFLUENCE_API_TOKEN': credential}}, 'capabilities': [{
            'capability_id': 'read', 'tool_name': 'confluence_get_page', 'constraints': {
                'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['main']},
                'path_prefixes': {'file_path': ['src']}}}], 'policy': {}}


def task(store, profile, name='task_a', calls=None):
    value = {'task_id': name, 'profile_id': profile['profile_id'], 'task_revision': 1,
             'state': 'running', 'calls': calls or [], 'profile_snapshot': copy.deepcopy(profile)}
    store.insert_task(value)
    return value


def test_profile_delete_previews_then_cascades_all_owned_data(store):
    credentials = FakeCredentials()
    first = store.put_profile('alpha', resources=[resource('old', 'credential:old')])
    first = store.put_profile('alpha', resources=[resource('private', 'credential:private'),
                                                 resource('shared', 'credential:shared')])
    other = store.put_profile('beta', resources=[resource('shared', 'credential:shared')])
    task(store, first)
    task(store, other, 'task_b')
    ref = store.write_record('task_a', 'report', {'text': 'delete me'})
    store.write_artifact('task_a', b'a,b\n1,2\n', 'csv', 'text/csv', 'csv')
    kept = store.write_record('task_b', 'report', {'text': 'keep me'})
    store.request_put('daeri_task_get:task_a', 'one', 'a', {'data': {'task_id': 'task_a'}})
    store.request_put('daeri_task_create:local', 'two', 'b', {'data': {'task_id': 'task_a'}})
    store.request_put('user_message:external', 'message', 'c', {}, profile_id=first['profile_id'])
    store.request_put('daeri_task_create:local', 'kept', 'd', {'data': {'task_id': 'task_b'}})
    before = store.get_profile('alpha')
    preview = setup.preview_profile_deletion(store, 'alpha')
    assert preview['state'] == 'ready' and preview['counts']['tasks'] == 1
    assert store.get_profile('alpha') == before
    assert store.read_record('task_a', ref)['payload'] == {'text': 'delete me'}
    result = setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=credentials)
    assert result['deleted'] is True
    assert [p['name'] for p in store.list_profiles()] == ['beta']
    assert store.get_profile('beta') == other
    assert store.read_record('task_b', kept)['payload'] == {'text': 'keep me'}
    assert credentials.values == {'credential:shared': 'SHARED'}
    assert store._db.execute('SELECT task_id FROM tasks').fetchall()[0][0] == 'task_b'
    assert [r[0] for r in store._db.execute('SELECT request_key FROM requests')] == ['kept']
    assert store._db.execute('PRAGMA foreign_key_check').fetchall() == []
    for path in (store.data_dir / 'profiles').glob('*.json'):
        assert json.loads(path.read_text())['profile_id'] != first['profile_id']
    for path in (store.data_dir / 'records').glob('*.json'):
        assert 'delete me' not in path.read_text()
    assert not list((store.data_dir / 'artifacts').iterdir())


def test_delete_failure_blocks_profile_and_can_resume_the_original_confirmation(store, monkeypatch):
    profile = store.put_profile('alpha')
    other = store.put_profile('beta')
    task(store, profile)
    ref = store.write_record('task_a', 'report', {'text': 'private'})
    preview = setup.preview_profile_deletion(store, 'alpha')
    unlink = Path.unlink
    def fail(path, *args, **kwargs):
        if path.parent.name == 'records':
            raise PermissionError('blocked')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(StoreError) as error:
            setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
        assert error.value.code == 'PROFILE_DELETE_PENDING'
    with pytest.raises(StoreError) as error:
        store.get_profile('alpha')
    assert error.value.code == 'PROFILE_DELETING'
    with pytest.raises(StoreError):
        store.get_task('task_a')
    with pytest.raises(StoreError):
        store.read_record('task_a', ref)
    with pytest.raises(StoreError):
        store.put_profile('alpha')
    assert store.get_profile('beta') == other
    assert [p['name'] for p in store.list_profiles()] == ['beta']
    assert setup.preview_profile_deletion(store, 'alpha')['state'] == 'deleting'
    assert setup.preview_profile_deletion(store, 'alpha')['last_error']['stage'] == 'files'
    assert setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())['deleted']


def test_stale_confirmation_cannot_delete_updated_or_recreated_profile(store):
    store.put_profile('alpha')
    preview = setup.preview_profile_deletion(store, 'alpha')
    updated = store.put_profile('alpha', core_context=['new context'])
    with pytest.raises(StoreError):
        setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert store.get_profile('alpha') == updated
    current = setup.preview_profile_deletion(store, 'alpha')
    setup.delete_profile(store, 'alpha', current['deletion_token'], credentials=FakeCredentials())
    recreated = store.put_profile('alpha')
    with pytest.raises(StoreError):
        setup.delete_profile(store, 'alpha', current['deletion_token'], credentials=FakeCredentials())
    assert store.get_profile('alpha') == recreated


def test_delete_waits_for_dispatched_call_and_active_local_resource_operation(store):
    profile = store.put_profile('alpha')
    pending = task(store, profile, calls=[{'call_state': 'pending', 'dispatched': True}])
    preview = setup.preview_profile_deletion(store, 'alpha')
    with pytest.raises(StoreError) as error:
        setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert error.value.code == 'PROFILE_BUSY'
    pending.update(calls=[], task_revision=2)
    store.update_task(pending, 1)
    with store.profile_operation(profile['profile_id']):
        with pytest.raises(StoreError) as error:
            setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
        assert error.value.code == 'PROFILE_BUSY'
    assert setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())['deleted']
    with pytest.raises(StoreError):
        with store.profile_operation(profile['profile_id']):
            raise AssertionError('A deleted profile cannot start a late operation')


def test_profile_delete_cli_defaults_to_preview_and_requires_its_confirmation(tmp_path):
    args = [sys.executable, '-m', 'daeri.cli', '--data-dir', str(tmp_path / 'private'), 'profile']
    subprocess.run([*args, 'create', 'alpha'], check=True, text=True, capture_output=True)
    preview = subprocess.run([*args, 'delete', 'alpha'], text=True, capture_output=True)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    value = json.loads(preview.stdout)
    shown = subprocess.run([*args, 'show', 'alpha'], text=True, capture_output=True)
    assert shown.returncode == 0
    deleted = subprocess.run([*args, 'delete', 'alpha', '--confirm', value['deletion_token']],
                             text=True, capture_output=True)
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr
    assert json.loads(deleted.stdout)['deleted'] is True


def test_resource_diagnosis_cannot_publish_after_profile_deletion(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from daeri.infrastructure.resources import ResourceRegistry
    profile = store.put_profile('alpha', resources=[resource('source', 'credential:private')])
    registry = ResourceRegistry(store)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(registry, '_credentials', lambda config: {})
    async def session(*args):
        return object(), {}
    async def tools(*args):
        entered.set()
        assert release.wait(5)
        return [{'name': 'confluence_get_page', 'inputSchema': {'type': 'object', 'properties': {}}}]
    monkeypatch.setattr(registry, '_session', session)
    monkeypatch.setattr(registry, '_tools', tools)
    preview = setup.preview_profile_deletion(store, 'alpha')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(registry.diagnose, profile, 'source')
        assert entered.wait(5)
        try:
            with pytest.raises(StoreError) as error:
                setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
            assert error.value.code == 'PROFILE_BUSY'
        finally:
            release.set()
        assert future.result(timeout=5)['ready'] is True
    setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert not list(registry.directory.glob('*.json'))


def test_delete_removes_superseded_record_bodies_in_the_same_store(store):
    profile = store.put_profile('alpha')
    task(store, profile)
    ref = store.write_record('task_a', 'call_result', {'text': 'OLD_PRIVATE_BODY'})
    old_path = store.data_dir / store._db.execute('SELECT path FROM records WHERE record_ref=?', (ref,)).fetchone()[0]
    store.restrict_record('task_a', ref, {'text': 'reduced'})
    assert old_path.exists()
    preview = setup.preview_profile_deletion(store, 'alpha')
    setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert not old_path.exists()
    assert not list((store.data_dir / 'records').iterdir())


def test_delete_allows_an_os_alias_above_the_managed_data_directory(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    alias = tmp_path / 'os-alias'
    alias.symlink_to(actual, target_is_directory=True)
    store = Store(alias / 'app' / 'private')
    try:
        store.put_profile('alpha')
        preview = setup.preview_profile_deletion(store, 'alpha')
        assert setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())['deleted']
    finally:
        store.close()


def test_another_profile_can_be_deleted_when_pending_profile_config_file_is_already_gone(store, monkeypatch):
    first = store.put_profile('alpha')
    store.put_profile('beta')
    preview = setup.preview_profile_deletion(store, 'alpha')
    config_path = store._db.execute('SELECT path FROM profiles WHERE name=?', ('alpha',)).fetchone()[0]
    unlink = Path.unlink
    def interrupted(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == store.data_dir / config_path:
            raise PermissionError('interrupted after unlink')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', interrupted)
        with pytest.raises(StoreError, match='PROFILE_DELETE_PENDING'):
            setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    other = setup.preview_profile_deletion(store, 'beta')
    assert setup.delete_profile(store, 'beta', other['deletion_token'], credentials=FakeCredentials())['deleted']
    assert setup.delete_profile(store, first['profile_id'], preview['deletion_token'], credentials=FakeCredentials())['deleted']


def test_a_managed_symlink_cannot_delete_an_external_file(store, tmp_path):
    profile = store.put_profile('alpha')
    task(store, profile)
    ref = store.write_record('task_a', 'report', {'text': 'owned'})
    path = store.data_dir / store._db.execute('SELECT path FROM records WHERE record_ref=?', (ref,)).fetchone()[0]
    outside = tmp_path / 'outside.txt'
    outside.write_text('keep outside')
    path.unlink()
    path.symlink_to(outside)
    preview = setup.preview_profile_deletion(store, 'alpha')
    with pytest.raises(StoreError, match='PROFILE_DELETE_PENDING'):
        setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert outside.read_text() == 'keep outside'


@pytest.mark.parametrize('layout', ['legacy', 'migrated', 'both'])
def test_delete_removes_profile_yaml_and_setup_metadata_preserving_other_profiles(store, layout):
    store.put_profile('alpha')
    other = store.put_profile('beta')
    config_dir = store.data_dir / 'config' / 'profiles'
    config_dir.mkdir(parents=True)
    (config_dir / 'beta.yaml').write_text('profile: beta\n')
    metadata = store.data_dir / 'setup' / 'alpha'
    metadata.mkdir(parents=True)
    (metadata / 'draft.json').write_text('{}')
    if layout in {'legacy', 'both'}:
        (metadata / 'setup.yaml').write_text('profile: alpha\n')
    if layout in {'migrated', 'both'}:
        (config_dir / 'alpha.yaml').write_text('profile: alpha\n')
    preview = setup.preview_profile_deletion(store, 'alpha')
    assert setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())['deleted']
    assert not (config_dir / 'alpha.yaml').exists()
    assert not metadata.exists()
    assert (config_dir / 'beta.yaml').read_text() == 'profile: beta\n'
    assert store.get_profile('beta') == other


@pytest.mark.parametrize('link', ['config', 'profiles', 'alpha.yaml'])
def test_profile_yaml_symlinks_keep_external_targets_and_allow_deletion_retry(store, tmp_path, link):
    store.put_profile('alpha')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep.txt').write_text('keep outside')
    config = store.data_dir / 'config'
    if link == 'config':
        (outside / 'profiles').mkdir()
        (outside / 'profiles' / 'alpha.yaml').write_text('keep yaml')
        linked = config
        target = outside
    elif link == 'profiles':
        config.mkdir()
        (outside / 'alpha.yaml').write_text('keep yaml')
        linked = config / 'profiles'
        target = outside
    else:
        (config / 'profiles').mkdir(parents=True)
        (outside / 'alpha.yaml').write_text('keep yaml')
        linked = config / 'profiles' / 'alpha.yaml'
        target = outside / 'alpha.yaml'
    linked.symlink_to(target, target_is_directory=link != 'alpha.yaml')
    before = {path.relative_to(outside): path.read_bytes() for path in outside.rglob('*') if path.is_file()}
    preview = setup.preview_profile_deletion(store, 'alpha')
    with pytest.raises(StoreError, match='PROFILE_DELETE_PENDING'):
        setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert setup.preview_profile_deletion(store, 'alpha')['state'] == 'deleting'
    assert {path.relative_to(outside): path.read_bytes() for path in outside.rglob('*') if path.is_file()} == before
    with pytest.raises(StoreError, match='PROFILE_DELETING'):
        store.get_profile('alpha')
    linked.unlink()
    assert setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())['deleted']
    assert {path.relative_to(outside): path.read_bytes() for path in outside.rglob('*') if path.is_file()} == before


def test_legacy_pending_deletion_includes_canonical_yaml_without_rereading_profile_body(store):
    profile = store.put_profile('alpha')
    config = store.data_dir / 'config' / 'profiles' / 'alpha.yaml'
    config.parent.mkdir(parents=True)
    config.write_text('profile: alpha\n')
    preview = setup.preview_profile_deletion(store, 'alpha')
    from daeri.config.profile_deletion import _manifest
    with store.transaction():
        row = store._db.execute('SELECT * FROM profiles WHERE profile_id=?', (profile['profile_id'],)).fetchone()
        manifest = _manifest(store, row)
        manifest['references'] = [ref for ref in manifest['references'] if ref != ['file', 'config/profiles/alpha.yaml']]
        store._db.execute("INSERT INTO profile_deletions VALUES(?,'deleting',?)",
                          (profile['profile_id'], json.dumps(manifest)))
        (store.data_dir / row['path']).unlink()
    result = setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert result['deleted'] is True and result['counts'] == preview['counts']
    assert not config.exists()


@pytest.mark.parametrize('reference', [
    'config/profiles/../keep.yaml', 'config/profiles/nested/alpha.yaml',
    'config/profiles/alpha.yml', 'config/profiles/.alpha.yaml',
])
def test_pending_deletion_accepts_only_exact_managed_profile_yaml_paths(store, reference):
    profile = store.put_profile('alpha')
    outside_scope = store.data_dir / 'config' / 'keep.yaml'
    outside_scope.parent.mkdir()
    outside_scope.write_text('keep configuration')
    preview = setup.preview_profile_deletion(store, 'alpha')
    from daeri.config.profile_deletion import _manifest
    with store.transaction():
        row = store._db.execute('SELECT * FROM profiles WHERE profile_id=?', (profile['profile_id'],)).fetchone()
        manifest = _manifest(store, row)
        manifest['references'].append(['file', reference])
        store._db.execute("INSERT INTO profile_deletions VALUES(?,'deleting',?)",
                          (profile['profile_id'], json.dumps(manifest)))
    with pytest.raises(StoreError, match='PROFILE_DELETE_PENDING'):
        setup.delete_profile(store, 'alpha', preview['deletion_token'], credentials=FakeCredentials())
    assert outside_scope.read_text() == 'keep configuration'
    assert setup.preview_profile_deletion(store, 'alpha')['state'] == 'deleting'
