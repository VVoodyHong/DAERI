import copy
import json
from pathlib import Path

import pytest

from daeri.onboarding import apply_draft, create_draft, validate_draft
from daeri.setup import delete_profile, preview_profile_deletion
from daeri.setup_yaml import read_yaml, write_yaml
from daeri.storage import Store, StoreError


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


def completed(store):
    draft = create_draft(store, 'production', {'primary': 'db-postgres'}, embedding='keyword_only')
    value = read_yaml(draft['path'])
    value['resources']['primary']['inputs'].update({
        'host': 'db.example.test', 'port': 5432, 'database': 'service',
        'username': 'reader'})
    Path(draft['path']).write_text(write_yaml(value), encoding='utf-8')
    return draft


class Credentials:
    def delete_if_present(self, reference):
        pass


def test_existing_profile_revision_cannot_change_after_yaml_review(store):
    store.put_profile('production', core_context=['original'])
    completed(store)
    token = validate_draft(store, 'production')['confirmation_token']
    changed = store.put_profile('production', core_context=['changed'])
    with pytest.raises(StoreError, match='REVISION_CONFLICT'):
        apply_draft(store, 'production', token)
    assert store.get_profile('production') == changed


def test_reviewed_schema_cannot_change_before_apply(store, monkeypatch):
    completed(store)
    token = validate_draft(store, 'production')['confirmation_token']
    from daeri import onboarding
    original = onboarding.setup_schema

    def changed_schema(recipe_id):
        schema = copy.deepcopy(original(recipe_id))
        schema['input_schema']['properties']['port']['maximum'] = 5431
        return schema

    monkeypatch.setattr(onboarding, 'setup_schema', changed_schema)
    with pytest.raises(StoreError, match='REVISION_CONFLICT'):
        apply_draft(store, 'production', token)
    assert store.list_profiles() == []


@pytest.mark.parametrize('token', [None, 1, '', 'x' * 64, '승인합니다'])
def test_invalid_confirmation_does_not_apply_yaml(store, token):
    completed(store)
    with pytest.raises(StoreError):
        apply_draft(store, 'production', token)
    assert store.list_profiles() == []


@pytest.mark.parametrize('selections', [[], {'primary': []}, {'primary': {}}, {1: 'db-postgres'}])
def test_malformed_resource_selection_returns_a_safe_error(store, selections):
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production', selections, embedding='keyword_only')
    assert store.list_profiles() == []


@pytest.mark.parametrize('embedding', [[], {}])
def test_malformed_embedding_selection_returns_a_safe_error(store, embedding):
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production', embedding=embedding)
    assert store.list_profiles() == []


@pytest.mark.parametrize('location', ['setup', 'profile'])
def test_symlinked_setup_parent_cannot_be_read_or_applied(store, tmp_path, location):
    draft = completed(store)
    token = validate_draft(store, 'production')['confirmation_token']
    managed = Path(draft['path']).parent if location == 'profile' else Path(draft['path']).parent.parent
    moved = tmp_path / 'outside'
    managed.rename(moved)
    managed.symlink_to(moved, target_is_directory=True)
    assert not validate_draft(store, 'production')['valid']
    with pytest.raises(StoreError):
        apply_draft(store, 'production', token)
    assert list(moved.rglob('setup.yaml'))
    assert store.list_profiles() == []


def test_draft_creation_does_not_write_through_a_symlink(store, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (store.data_dir / 'setup').symlink_to(outside, target_is_directory=True)
    with pytest.raises(StoreError):
        create_draft(store, 'production', {'primary': 'db-postgres'}, embedding='keyword_only')
    assert list(outside.iterdir()) == []


def test_missing_yaml_is_a_safe_validation_failure(store):
    draft = completed(store)
    token = validate_draft(store, 'production')['confirmation_token']
    Path(draft['path']).unlink()
    assert not validate_draft(store, 'production')['valid']
    with pytest.raises(StoreError):
        apply_draft(store, 'production', token)
    assert store.list_profiles() == []


@pytest.mark.parametrize('location', ['setup', 'profile', 'setup.yaml', 'draft.json'])
def test_draft_status_rejects_symlinks_and_preserves_their_targets(store, tmp_path, location):
    from daeri.onboarding import draft_status
    draft = completed(store)
    directory = Path(draft['path']).parent
    managed = (directory.parent if location == 'setup' else directory if location == 'profile'
               else directory / location)
    outside = tmp_path / 'outside'
    is_directory = managed.is_dir()
    managed.rename(outside)
    managed.symlink_to(outside, target_is_directory=is_directory)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        draft_status(store, 'production')
    assert outside.exists() and managed.is_symlink()


@pytest.mark.parametrize('operation', ['lstat', 'open'])
def test_draft_status_reports_access_errors_instead_of_new_setup(store, monkeypatch, operation):
    from daeri.onboarding import draft_status
    draft = completed(store)
    path = Path(draft['path'])
    before = path.read_bytes()
    original = getattr(Path, operation)

    def inaccessible(self, *args, **kwargs):
        if self == path:
            raise PermissionError('PRIVATE_SENTINEL')
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, inaccessible)
        with pytest.raises(StoreError, match='STORE_UNAVAILABLE') as error:
            draft_status(store, 'production')
        assert 'PRIVATE_SENTINEL' not in str(error.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', [
    lambda value: value['resources']['primary']['inputs'].update({'PRIVATE_SENTINEL': 'x'}),
    lambda value: value['resources']['primary']['inputs'].update(password='PRIVATE_SENTINEL'),
    lambda value: value.update(embedding={'provider': 'openai_compatible',
        'endpoint': 'https://alice:PRIVATE_SENTINEL@embedding.example.test/v1/embeddings',
        'model': 'company', 'credential_ref': 'credential:production.embedding', 'allow_remote': True}),
])
def test_invalid_fields_and_authentication_values_are_not_echoed(store, change):
    draft = completed(store)
    value = read_yaml(draft['path'])
    change(value)
    Path(draft['path']).write_text(write_yaml(value), encoding='utf-8')
    result = validate_draft(store, 'production')
    assert not result['valid']
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
    assert store.list_profiles() == []


def test_deleted_profile_cannot_be_recreated_by_its_old_setup_confirmation(store):
    completed(store)
    apply_draft(store, 'production', validate_draft(store, 'production')['confirmation_token'])
    token = validate_draft(store, 'production')['confirmation_token']
    delete_profile(store, 'production', preview_profile_deletion(store, 'production')['deletion_token'],
                   credentials=Credentials())
    replacement = store.put_profile('production', core_context=['replacement'])
    with pytest.raises(StoreError):
        apply_draft(store, 'production', token)
    assert store.get_profile('production') == replacement


def test_profile_pending_deletion_cannot_be_updated_from_a_setup_draft(store):
    completed(store)
    profile = apply_draft(store, 'production', validate_draft(store, 'production')['confirmation_token'])
    token = validate_draft(store, 'production')['confirmation_token']

    class UnavailableCredentials:
        def delete_if_present(self, reference):
            raise StoreError('CREDENTIAL_STORE_UNAVAILABLE', 'Credential store unavailable.')

    with pytest.raises(StoreError, match='PROFILE_DELETE_PENDING'):
        delete_profile(store, 'production', preview_profile_deletion(store, 'production')['deletion_token'],
                       credentials=UnavailableCredentials())
    with pytest.raises(StoreError):
        apply_draft(store, 'production', token)
    row = store._db.execute('SELECT revision FROM profiles WHERE profile_id=?',
                            (profile['profile_id'],)).fetchone()
    assert row[0] == profile['revision']


@pytest.mark.parametrize('field,value', [
    ('categories', [{}]), ('context_sources', [{}]), ('retention', {}),
])
def test_invalid_nested_policy_returns_a_field_issue_without_activating_profile(store, field, value):
    draft = completed(store)
    path = Path(draft['path'])
    document = read_yaml(path)
    memory = {'categories': [], 'source_fields': [], 'context_sources': [],
              'retention': 'none', 'allow_model_recall': False}
    memory[field] = value
    document['resources']['primary']['policy'] = {'memory': memory}
    path.write_text(write_yaml(document), encoding='utf-8')
    result = validate_draft(store, 'production')
    assert not result['valid'] and result['confirmation_token'] is None
    assert any(i['field'].startswith('resources.primary') for i in result['issues'])
    assert store.list_profiles() == []
