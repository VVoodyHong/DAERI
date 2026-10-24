import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from daeri.onboarding import apply_draft, create_draft, draft_status, validate_draft
from daeri.setup_yaml import read_yaml, write_yaml
from daeri.storage import Store, StoreError


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


def paths(store):
    return (store.data_dir / 'config' / 'profiles' / 'production.yaml',
            store.data_dir / 'setup' / 'production' / 'draft.json',
            store.data_dir / 'setup' / 'production' / 'setup.yaml')


def legacy_draft(store):
    draft = create_draft(store, 'production', recipes=['db-postgres'], embedding='keyword_only')
    active = Path(draft['path'])
    value = read_yaml(active)
    value['resources']['db']['inputs'].update(
        host='db.example.test', database='service', username='reader')
    active.write_text('# keep this comment\n' + write_yaml(value), encoding='utf-8')
    canonical, metadata, legacy = paths(store)
    if active != legacy:
        legacy.parent.mkdir(parents=True, exist_ok=True)
        active.rename(legacy)
    return canonical, metadata, legacy


def test_user_yaml_and_internal_metadata_have_separate_private_locations(store):
    draft = create_draft(store, 'production', recipes=['db-postgres'])
    canonical, metadata, legacy = paths(store)
    assert draft['path'] == str(canonical)
    assert canonical.is_file() and metadata.is_file() and not legacy.exists()
    if os.name != 'nt':
        assert canonical.stat().st_mode & 0o777 == 0o600
        assert metadata.stat().st_mode & 0o777 == 0o600
        for directory in (canonical.parent, canonical.parent.parent, metadata.parent, metadata.parent.parent):
            assert directory.stat().st_mode & 0o777 == 0o700


def test_empty_status_is_read_only_and_does_not_initialize_storage(tmp_path):
    location = tmp_path / 'absent'
    assert draft_status(SimpleNamespace(data_dir=location), 'production') == {
        'profile': 'production', 'state': 'new', 'path': None}
    assert not location.exists()


def test_legacy_status_and_validation_preserve_original_until_explicit_migration(store):
    canonical, metadata, legacy = legacy_draft(store)
    before = legacy.read_bytes(), metadata.read_bytes()
    assert draft_status(store, 'production')['path'] == str(legacy)
    assert validate_draft(store, 'production')['valid']
    assert (legacy.read_bytes(), metadata.read_bytes()) == before
    assert not canonical.exists()


def test_explicit_resume_migrates_yaml_bytes_and_preserves_review_token(store):
    canonical, metadata, legacy = legacy_draft(store)
    before = legacy.read_bytes()
    base = json.loads(metadata.read_text())['base']
    token = validate_draft(store, 'production')['confirmation_token']
    resumed = create_draft(store, 'production', recipes=['db-postgres'])
    assert resumed['existing'] and resumed['path'] == str(canonical)
    assert canonical.read_bytes() == before and not legacy.exists()
    assert json.loads(metadata.read_text())['base'] == base
    assert validate_draft(store, 'production')['confirmation_token'] == token
    profile = apply_draft(store, 'production', token)
    assert profile['name'] == 'production'
    assert draft_status(store, 'production')['path'] == str(canonical)


def test_legacy_apply_remains_available_before_migration(store):
    canonical, _, legacy = legacy_draft(store)
    token = validate_draft(store, 'production')['confirmation_token']
    assert apply_draft(store, 'production', token)['name'] == 'production'
    assert legacy.exists() and not canonical.exists()


@pytest.mark.parametrize('files', [('canonical',), ('legacy',), ('metadata',), ('canonical', 'legacy', 'metadata')])
def test_partial_or_conflicting_files_require_recovery_without_overwriting(store, files):
    canonical, metadata, legacy = paths(store)
    values = dict(canonical=canonical, metadata=metadata, legacy=legacy)
    for name in files:
        values[name].parent.mkdir(parents=True, exist_ok=True)
        values[name].write_bytes(name.encode())
    before = {name: values[name].read_bytes() for name in files}
    for action in (lambda: draft_status(store, 'production'),
                   lambda: create_draft(store, 'production')):
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            action()
        assert {name: values[name].read_bytes() for name in files} == before


@pytest.mark.parametrize('location', ['config', 'config/profiles', 'config/profiles/production.yaml',
                                    'setup', 'setup/production', 'setup/production/draft.json',
                                    'setup/production/setup.yaml'])
def test_every_new_and_legacy_managed_path_rejects_symlinks(store, tmp_path, location):
    managed = store.data_dir / location
    managed.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    managed.symlink_to(outside, target_is_directory=True)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        draft_status(store, 'production')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production')
    assert not list(outside.iterdir())


def test_failed_migration_preserves_original_yaml_and_metadata(store, monkeypatch):
    canonical, metadata, legacy = legacy_draft(store)
    before = legacy.read_bytes(), metadata.read_bytes()
    original = os.link

    def unavailable(source, destination, **kwargs):
        if Path(source) == legacy:
            raise OSError('PRIVATE_SENTINEL')
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, 'link', unavailable)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE') as error:
        create_draft(store, 'production')
    assert 'PRIVATE_SENTINEL' not in str(error.value)
    assert (legacy.read_bytes(), metadata.read_bytes()) == before
    assert not canonical.exists()


def test_failed_legacy_removal_restores_the_original_pair(store, monkeypatch):
    canonical, metadata, legacy = legacy_draft(store)
    before = legacy.read_bytes(), metadata.read_bytes()
    original = Path.unlink

    def unavailable(self, *args, **kwargs):
        if self == legacy:
            raise PermissionError('PRIVATE_SENTINEL')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', unavailable)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        create_draft(store, 'production')
    assert (legacy.read_bytes(), metadata.read_bytes()) == before
    assert not canonical.exists()


def test_metadata_write_failure_after_migration_keeps_a_resumable_canonical_pair(store, monkeypatch):
    from daeri import onboarding
    canonical, metadata, legacy = legacy_draft(store)
    before = legacy.read_bytes(), metadata.read_bytes()
    original = onboarding._write

    def unavailable(path, text):
        if path == metadata:
            raise StoreError('STORE_UNAVAILABLE', 'Simulated metadata failure.')
        return original(path, text)

    monkeypatch.setattr(onboarding, '_write', unavailable)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        create_draft(store, 'production')
    assert (canonical.read_bytes(), metadata.read_bytes()) == before
    assert not legacy.exists()
    assert draft_status(store, 'production')['path'] == str(canonical)


def test_migration_preserves_unapplied_inline_credentials_without_exposing_them(store):
    canonical, _, legacy = legacy_draft(store)
    document = read_yaml(legacy)
    document['resources']['db']['credentials']['password'] = 'PRIVATE_SENTINEL'
    legacy.write_text('# retain comment\n' + write_yaml(document), encoding='utf-8')
    before = legacy.read_bytes()
    result = create_draft(store, 'production')
    assert canonical.read_bytes() == before
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


@pytest.mark.parametrize('location', ['config', 'config/profiles', 'setup', 'setup/production'])
def test_non_directory_ancestor_requires_recovery_even_when_the_other_branch_is_missing(store, location):
    path = store.data_dir / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'original')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        draft_status(store, 'production')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production')
    assert path.read_bytes() == b'original'


@pytest.mark.parametrize('legacy', [False, True])
def test_validation_errors_identify_the_active_yaml_filename(store, legacy):
    if legacy:
        _, _, path = legacy_draft(store)
    else:
        path = Path(create_draft(store, 'production')['path'])
    path.write_text('invalid', encoding='utf-8')
    result = validate_draft(store, 'production')
    assert not result['valid']
    assert result['issues'][0]['field'] == path.name


@pytest.mark.parametrize('name', [None, '../escape', 'password=PRIVATE_SENTINEL'])
def test_invalid_profile_in_validation_returns_a_safe_issue(store, name):
    result = validate_draft(store, name)
    assert not result['valid'] and result['confirmation_token'] is None
    assert result['issues'][0]['field'] == 'profile'
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_missing_valid_profile_uses_the_expected_yaml_filename_in_validation(store):
    result = validate_draft(store, 'production')
    assert not result['valid']
    assert result['issues'][0]['field'] == 'production.yaml'
