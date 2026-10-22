import copy
import json
import os

import pytest

from daeri.storage import Store, StoreError
from daeri.onboarding import create_draft, validate_draft, apply_draft, selection_catalog


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


DB = {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'}


def edit(draft, change):
    from daeri.setup_yaml import read_yaml, write_yaml
    from pathlib import Path
    path = Path(draft['path'])
    value = read_yaml(path)
    change(value)
    path.write_text(write_yaml(value), encoding='utf-8')


def completed(store, selections=None):
    draft = create_draft(store, 'production', selections or {'primary': 'db-postgres'}, embedding='keyword_only')
    edit(draft, lambda value: value['resources']['primary']['inputs'].update(DB))
    return draft


def test_catalog_groups_all_roles_and_available_products(monkeypatch):
    groups = selection_catalog()['groups']
    assert {g['type'] for g in groups} == {'DB', 'Cache', 'Infra', 'Source', 'Document'}
    from daeri import onboarding
    catalog = onboarding.load_catalog()
    monkeypatch.setattr(onboarding, 'load_catalog', lambda: catalog | {
        'db-other': dict(catalog['db-postgres'], display_name='Another DB')})
    db = selection_catalog()['groups'][0]
    assert [p['display_name'] for p in db['products']] == ['PostgreSQL', 'Another DB']
    assert db['description'] == '사용 가능한 제품: PostgreSQL, Another DB'


def test_draft_contains_only_selected_schema_fields_and_no_service_description(store):
    draft = create_draft(store, 'production', {'primary': 'db-postgres'}, embedding='keyword_only')
    from daeri.setup_yaml import read_yaml
    value = read_yaml(draft['path'])
    assert set(value) == {'profile', 'resources', 'embedding'}
    assert set(value['resources']) == {'primary'}
    assert value['resources']['primary']['inputs']['port'] == 5432
    assert value['resources']['primary']['inputs']['host'] is None
    result = validate_draft(store, 'production')
    assert not result['complete'] and result['issues']
    assert store.list_profiles() == []
    if os.name != 'nt':
        from pathlib import Path
        assert Path(draft['path']).stat().st_mode & 0o777 == 0o600


def test_whole_yaml_apply_is_atomic_and_idempotent(store):
    draft = completed(store, {'primary': 'db-postgres', 'secondary': 'db-postgres'})
    result = validate_draft(store, 'production')
    assert not result['valid'] and result['confirmation_token'] is None
    assert store.list_profiles() == []
    edit(draft, lambda value: value['resources']['secondary']['inputs'].update(DB))
    result = validate_draft(store, 'production')
    assert result['complete'] and result['confirmation_token']
    assert 'command' not in json.dumps(result)
    profile = apply_draft(store, 'production', result['confirmation_token'])
    assert profile['revision'] == 1 and len(profile['resources']) == 2
    assert profile['core_context'] == [] and profile['retention_days'] is None
    assert profile['embedding']['provider'] == 'keyword_only'
    assert all(r['policy'] == {} for r in profile['resources'])
    again = validate_draft(store, 'production')
    assert apply_draft(store, 'production', again['confirmation_token'])['revision'] == 1


def test_all_current_resource_categories_share_the_same_yaml_apply_flow(store):
    from daeri.setup import load_catalog
    selections = {recipe: recipe for recipe in load_catalog()}
    draft = create_draft(store, 'production', selections, embedding='keyword_only')
    answers = {
        'db-postgres': DB,
        'cache-redis': {'host': 'cache.example.test'},
        'infra-aws': {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': '111111111111'},
        'source-gitlab': {'server': 'https://git.example.test'},
        'document-confluence': {'server': 'https://docs.example.test/wiki', 'username': 'reader@example.test'},
    }
    edit(draft, lambda value: [value['resources'][name]['inputs'].update(fields)
                               for name, fields in answers.items()])
    result = validate_draft(store, 'production')
    assert result['valid'], result['issues']
    profile = apply_draft(store, 'production', result['confirmation_token'])
    assert profile['revision'] == 1 and len(profile['resources']) == 5
    assert {r['type'] for r in profile['resources']} == {'DB', 'Cache', 'Infra', 'Source', 'Document'}
    assert all(r['policy'] == {} for r in profile['resources'])


def test_yaml_syntax_error_reports_position_without_the_raw_value(store):
    from pathlib import Path
    draft = completed(store)
    Path(draft['path']).write_text('profile: production\nresources: [PRIVATE_SENTINEL', encoding='utf-8')
    result = validate_draft(store, 'production')
    assert not result['valid'] and '행' in result['issues'][0]['message']
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_validation_token_binds_the_reviewed_bytes(store):
    draft = completed(store)
    token = validate_draft(store, 'production')['confirmation_token']
    edit(draft, lambda value: value['resources']['primary']['inputs'].update(port=5433))
    with pytest.raises(StoreError, match='REVISION_CONFLICT'):
        apply_draft(store, 'production', token)
    assert not store.list_profiles()


def test_existing_changes_and_new_profile_creation_conflict_until_explicit_refresh(store):
    completed(store)
    token = validate_draft(store, 'production')['confirmation_token']
    profile = store.put_profile('production', core_context=['keep'], retention_days=15)
    with pytest.raises(StoreError, match='REVISION_CONFLICT'):
        apply_draft(store, 'production', token)
    create_draft(store, 'production', refresh=True)
    fresh = validate_draft(store, 'production')
    result = apply_draft(store, 'production', fresh['confirmation_token'])
    assert result['revision'] == profile['revision'] + 1
    assert result['core_context'] == ['keep'] and result['retention_days'] == 15
    assert result['long_term_memory_days'] is None


def test_reopening_preserves_edits_and_appends_only_new_selections(store):
    draft = completed(store)
    before = open(draft['path']).read()
    assert create_draft(store, 'production')['path'] == draft['path']
    assert open(draft['path']).read() == before
    create_draft(store, 'production', {'primary': 'db-postgres', 'docs': 'document-confluence'})
    from daeri.setup_yaml import read_yaml
    value = read_yaml(draft['path'])
    assert value['resources']['primary']['inputs'] == DB
    assert value['resources']['docs']['adapter'] == 'document-confluence'
    with pytest.raises(StoreError):
        create_draft(store, 'production', {'primary': 'cache-redis'})


def test_missing_blocks_and_fields_preserve_existing_values_and_policies(store):
    draft = completed(store)
    preview = validate_draft(store, 'production')
    profile = apply_draft(store, 'production', preview['confirmation_token'])
    config = {k: copy.deepcopy(profile[k]) for k in ('name', 'core_context', 'resources', 'embedding')}
    config['core_context'] = ['existing service']
    config['resources'][0]['policy'] = {'allowed_model_fields': []}
    store.put_profile(**config)
    create_draft(store, 'production', refresh=True)
    edit(draft, lambda value: value.update(resources={}))
    before = store.get_profile('production')
    result = apply_draft(store, 'production', validate_draft(store, 'production')['confirmation_token'])
    assert result == before
    edit(draft, lambda value: value.update(resources={'primary': {
        'adapter': 'db-postgres', 'inputs': {'port': 5433}}}))
    result = apply_draft(store, 'production', validate_draft(store, 'production')['confirmation_token'])
    assert result['resources'][0]['target']['database'] == 'service'
    assert result['resources'][0]['policy'] == {'allowed_model_fields': []}


@pytest.mark.parametrize('change', [
    lambda v: v.update(profile='other'),
    lambda v: v.update(unknown='PRIVATE_SENTINEL'),
    lambda v: v['resources']['primary']['inputs'].update(password='PRIVATE_SENTINEL'),
    lambda v: v['resources']['primary']['inputs'].update(port='5432'),
    lambda v: v['resources']['primary'].update(adapter='cache-redis'),
    lambda v: v.update(retention_days=0),
    lambda v: v.update(memory_policy={'model_allowed': 'yes'}),
])
def test_invalid_yaml_never_activates_or_echoes_values(store, change):
    draft = completed(store)
    edit(draft, change)
    result = validate_draft(store, 'production')
    assert not result['valid'] and result['confirmation_token'] is None
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
    assert not store.list_profiles()


def test_company_api_template_does_not_require_local_model_or_expose_secrets(store):
    draft = create_draft(store, 'production', embedding='openai_compatible')
    from daeri.setup_yaml import read_yaml
    value = read_yaml(draft['path'])
    assert value['embedding'] == {'provider': 'openai_compatible', 'endpoint': None,
        'model': None, 'credential_ref': 'credential:production.embedding', 'allow_remote': False}
    edit(draft, lambda v: v['embedding'].update(endpoint='https://embedding.example.test/v1/embeddings', model='company-model', allow_remote=True))
    result = validate_draft(store, 'production')
    assert result['valid']
    profile = apply_draft(store, 'production', result['confirmation_token'])
    assert profile['embedding']['provider'] == 'openai_compatible'


def test_explicit_null_embedding_does_not_switch_to_local(store):
    draft = completed(store)
    edit(draft, lambda value: value.update(embedding=None))
    assert not validate_draft(store, 'production')['valid']


def test_missing_required_field_is_named_without_echoing_other_values(store):
    draft = completed(store)
    edit(draft, lambda value: value['resources']['primary']['inputs'].pop('host'))
    result = validate_draft(store, 'production')
    assert 'resources.primary.inputs.host' in [i['field'] for i in result['issues']]


def test_malformed_block_cannot_refresh_the_draft_base(store):
    draft = completed(store)
    edit(draft, lambda value: value['resources'].update(primary=None))
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production', refresh=True)


def test_symlink_draft_is_rejected(store, tmp_path):
    draft = completed(store)
    from pathlib import Path
    path = Path(draft['path'])
    outside = tmp_path / 'copy.yaml'
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    assert not validate_draft(store, 'production')['valid']
    assert outside.exists()


def test_profile_deletion_removes_owned_draft_and_keeps_external_copy(store, tmp_path):
    draft = completed(store)
    apply_draft(store, 'production', validate_draft(store, 'production')['confirmation_token'])
    from pathlib import Path
    from daeri.setup import preview_profile_deletion, delete_profile
    outside = tmp_path / 'backup.yaml'
    outside.write_bytes(Path(draft['path']).read_bytes())
    class Credentials:
        def delete_if_present(self, ref): pass
    delete_profile(store, 'production', preview_profile_deletion(store, 'production')['deletion_token'], credentials=Credentials())
    assert not Path(draft['path']).parent.exists()
    assert outside.exists()
