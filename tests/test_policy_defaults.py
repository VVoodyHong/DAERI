"""Profile limits are optional; explicit restrictions survive configuration edits."""

from copy import deepcopy

import pytest

from daeri import policy
from daeri.onboarding import apply_draft, create_draft, validate_draft
from daeri.planning import validate_plan
from daeri.setup import export_profile, import_profile, profile_template
from daeri.setup_yaml import read_yaml, write_yaml
from daeri.storage import Store, StoreError
from test_planning import Registry, inputs
from test_setup import config_file, resource


ROOT = {'segments': []}
CATEGORIES = {'service_rule', 'resource_structure', 'procedure', 'lesson'}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'private')
    yield value
    value.close()


@pytest.mark.parametrize('supplied', ['omitted', None, {}])
def test_resource_limit_default_defers_disclosure_to_the_approved_plan(supplied):
    configured = {} if supplied == 'omitted' else {'policy': supplied}
    ceiling = policy.effective_resource_policy(configured)
    for field in ('allowed_model_fields', 'export_fields', 'retained_fields'):
        assert ceiling[field] == [ROOT]
    assert set(ceiling['memory']['categories']) == CATEGORIES
    assert ceiling['memory']['source_fields'] == [ROOT]
    assert set(ceiling['memory']['context_sources']) == {'request', 'clarification'}
    assert ceiling['memory']['retention'] == 'long_term'
    assert ceiling['memory']['allow_model_recall'] is True
    ceiling['allowed_model_fields'].clear()
    assert policy.effective_resource_policy(configured)['allowed_model_fields'] == [ROOT]


@pytest.mark.parametrize('restriction', [{'allowed_model_fields': []}, {'memory': {}}])
def test_explicit_resource_limits_keep_their_previous_denial_semantics(restriction):
    assert policy.effective_resource_policy({'policy': restriction}) == restriction
    profile, draft = inputs()
    profile['resources'][0]['policy'] = restriction
    with pytest.raises(StoreError, match='PLAN_INVALID'):
        validate_plan(profile, draft, Registry(), [])


@pytest.mark.parametrize('supplied', ['omitted', None, {}])
def test_plan_scope_is_valid_with_no_extra_profile_ceiling(supplied):
    profile, draft = inputs()
    if supplied == 'omitted':
        profile['resources'][0].pop('policy')
    else:
        profile['resources'][0]['policy'] = supplied
    assert validate_plan(profile, draft, Registry(), []) == ['contract']
    draft['output']['resource_rules'][0] = None
    with pytest.raises(StoreError, match='PLAN_INVALID'):
        validate_plan(profile, draft, Registry(), [])


def test_default_profile_memory_is_current_target_bound_and_does_not_mutate_storage(store):
    configured = resource()
    configured['policy'] = None
    profile = store.put_profile('service', resources=[configured])
    assert profile['memory_policy'] is None
    ceiling = policy.effective_profile_memory_policy(profile)
    assert set(ceiling['categories']) == CATEGORIES
    assert ceiling['instance_ids'] == ['source-one']
    assert set(ceiling['target_bindings']) == {'source-one'}
    assert all(ceiling[field] is True for field in
               ('model_allowed', 'retain_allowed', 'long_term_allowed', 'exports_allowed'))
    changed = deepcopy(profile)
    changed['resources'][0]['target']['project'] = 'team/other'
    assert policy.effective_profile_memory_policy(changed)['target_bindings'] != ceiling['target_bindings']
    assert store.get_profile('service')['memory_policy'] is None
    assert policy.effective_profile_memory_policy(dict(profile, memory_policy={})) == ceiling


@pytest.mark.parametrize('supplied', ['omitted', None, {}])
def test_import_export_normalizes_legacy_empty_limits_to_plan_defaults(store, tmp_path, supplied):
    configured = resource()
    if supplied == 'omitted':
        configured.pop('policy')
    else:
        configured['policy'] = supplied
    config = {'name': 'service', 'resources': [configured]}
    if supplied != 'omitted':
        config['memory_policy'] = supplied
    profile = import_profile(store, config_file(tmp_path, config))
    expected = None
    assert profile['memory_policy'] == expected
    assert profile['resources'][0].get('policy') == expected
    exported = export_profile(store, 'service')
    assert exported['memory_policy'] == expected
    assert import_profile(store, config_file(tmp_path, exported))['memory_policy'] == expected


def test_setup_defaults_to_plan_limits_and_yaml_can_restore_or_restrict_them(store):
    from pathlib import Path

    assert profile_template(store, 'db-postgres', 'service', 'db')['memory_policy'] is None
    assert profile_template(store, 'db-postgres', 'service', 'db')['resources'][0]['policy'] is None
    draft = create_draft(store, 'service', {'db': 'db-postgres'}, embedding='keyword_only')
    path = Path(draft['path'])
    document = read_yaml(path)
    document['resources']['db']['inputs'].update(
        host='db.example.test', database='service', username='reader')
    path.write_text(write_yaml(document), encoding='utf-8')
    checked = validate_draft(store, 'service')
    assert checked['valid'], checked['issues']
    first = apply_draft(store, 'service', checked['confirmation_token'])
    assert first['memory_policy'] is None and first['resources'][0]['policy'] is None
    for resource_limit, memory_limit in (({'allowed_model_fields': []}, {'model_allowed': False}), (None, None)):
        document = read_yaml(path)
        document['memory_policy'] = memory_limit
        document['resources']['db']['policy'] = resource_limit
        path.write_text(write_yaml(document), encoding='utf-8')
        checked = validate_draft(store, 'service')
        assert checked['valid'], checked['issues']
        before = store.get_profile('service')
        assert before['memory_policy'] != memory_limit
        saved = apply_draft(store, 'service', checked['confirmation_token'])
        expected_memory = None if memory_limit is None else {**memory_limit, 'target_bindings': {}}
        assert saved['memory_policy'] == expected_memory and saved['resources'][0]['policy'] == resource_limit
        assert saved['revision'] == before['revision'] + 1


@pytest.mark.parametrize('legacy_flag', [False, True])
def test_applying_legacy_embedding_yaml_keeps_selected_api_and_removes_the_old_switch(store, legacy_flag):
    from pathlib import Path
    from daeri.credentials import Credentials
    from test_onboarding_credentials import Vault

    draft = create_draft(store, 'service', embedding='api', embedding_adapter='openai_compatible')
    path = Path(draft['path'])
    document = read_yaml(path)
    document['embedding'].update(endpoint='https://embedding.example.test/v1/embeddings',
        model='company-model', allow_remote=legacy_flag, credentials={'api_key': 'private-test-key'})
    path.write_text(write_yaml(document), encoding='utf-8')
    checked = validate_draft(store, 'service')
    assert checked['valid'], checked['issues']
    vault = Vault()
    saved = apply_draft(store, 'service', checked['confirmation_token'], credentials=Credentials(backend=vault))
    embedding = read_yaml(path)['embedding']
    assert 'allow_remote' not in embedding
    assert saved['embedding']['provider'] == embedding['provider'] == 'api'
    assert vault.writes == ['service.embedding']


def test_selecting_api_again_reuses_legacy_draft_connection_values_and_enables_the_choice(store):
    from pathlib import Path

    draft = create_draft(store, 'service', embedding='api', embedding_adapter='openai_compatible')
    path = Path(draft['path'])
    document = read_yaml(path)
    document['embedding'].update(endpoint='https://embedding.example.test/v1/embeddings',
        model='company-model', allow_remote=False)
    path.write_text(write_yaml(document), encoding='utf-8')
    create_draft(store, 'service', embedding='api', embedding_adapter='openai_compatible')
    selected = read_yaml(path)['embedding']
    assert 'allow_remote' not in selected
    assert selected['endpoint'] == document['embedding']['endpoint']
    checked = validate_draft(store, 'service')
    assert checked['valid'], checked['issues']
    saved = apply_draft(store, 'service', checked['confirmation_token'])
    assert saved['embedding']['provider'] == 'api'


def test_legacy_profile_keeps_its_api_selection_without_an_extra_setup_step(store):
    profile = store.put_profile('service', embedding={'provider': 'keyword_only'})
    legacy = deepcopy(profile)
    legacy['embedding'] = {'provider': 'api', 'adapter': 'openai_compatible',
        'endpoint': 'https://embedding.example.test/v1/embeddings', 'model': 'company-model',
        'credential_ref': 'credential:shared.embedding', 'allow_remote': False}
    with store.transaction():
        path, size, digest = store._prepare('profiles', legacy)
        store._db.execute('UPDATE profiles SET path=?, size=?, sha256=? WHERE profile_id=?',
                          (path, size, digest, profile['profile_id']))
    draft = create_draft(store, 'service', embedding='api', embedding_adapter='openai_compatible')
    selected = read_yaml(draft['path'])['embedding']
    assert 'allow_remote' not in selected
    assert selected['endpoint'] == legacy['embedding']['endpoint']
    assert selected['credential_ref'] == 'credential:shared.embedding'
    assert store.get_profile('service')['embedding']['provider'] == 'api'
    checked = validate_draft(store, 'service')
    assert checked['valid'], checked['issues']
    saved = apply_draft(store, 'service', checked['confirmation_token'])
    assert saved['embedding']['provider'] == 'api' and 'allow_remote' not in saved['embedding']


def test_existing_profile_import_preserves_omitted_limits_but_explicit_null_restores_plan_defaults(store, tmp_path):
    configured = resource()
    configured['policy'] = {'allowed_model_fields': []}
    original = store.put_profile('service', resources=[configured], memory_policy={'model_allowed': False})
    edited = deepcopy(configured)
    edited.pop('policy')
    edited['target']['project'] = 'team/changed'
    new_resource = deepcopy(edited)
    new_resource['instance_id'] = 'source-two'
    supplied = {'name': 'service', 'resources': [edited, new_resource]}
    saved = import_profile(store, config_file(tmp_path, supplied))
    assert saved['memory_policy'] == original['memory_policy']
    assert saved['memory_policy']['model_allowed'] is False
    assert saved['resources'][0]['policy'] == {'allowed_model_fields': []}
    assert saved['resources'][1].get('policy') is None
    supplied['memory_policy'] = None
    supplied['resources'][0]['policy'] = None
    restored = import_profile(store, config_file(tmp_path, supplied))
    assert restored['memory_policy'] is None and restored['resources'][0]['policy'] is None


def test_reading_legacy_factory_defaults_preserves_revision_and_file_integrity(store):
    original = store.put_profile('service', resources=[resource()], embedding={'provider': 'keyword_only'})
    legacy = deepcopy(original)
    legacy['memory_policy'] = {}
    legacy['resources'][0]['policy'] = {}
    legacy['embedding'] = {'provider': 'api', 'adapter': 'openai_compatible',
        'endpoint': 'https://embedding.example.test/v1/embeddings', 'model': 'company-model',
        'credential_ref': 'credential:shared.embedding', 'allow_remote': False}
    with store.transaction():
        path, size, digest = store._prepare('profiles', legacy)
        store._db.execute('UPDATE profiles SET path=?, size=?, sha256=? WHERE profile_id=?',
                          (path, size, digest, original['profile_id']))
    from pathlib import Path
    file = Path(store.data_dir, path)
    contents = file.read_bytes()
    selected = store.get_profile('service')
    assert selected['revision'] == original['revision']
    assert selected['memory_policy'] is None and selected['resources'][0]['policy'] is None
    assert selected['embedding']['provider'] == 'api' and 'allow_remote' not in selected['embedding']
    assert file.read_bytes() == contents
    assert policy.effective_resource_policy(legacy['resources'][0])['allowed_model_fields'] == [ROOT]
    assert policy.effective_profile_memory_policy(legacy)['instance_ids'] == ['source-one']


def test_empty_legacy_permissions_are_removed_from_yaml_without_a_redundant_revision(store):
    from pathlib import Path

    draft = create_draft(store, 'service', {'db': 'db-postgres'}, embedding='keyword_only')
    path = Path(draft['path'])
    document = read_yaml(path)
    document['resources']['db']['inputs'].update(host='db.example.test', database='service', username='reader')
    path.write_text(write_yaml(document), encoding='utf-8')
    checked = validate_draft(store, 'service')
    original = apply_draft(store, 'service', checked['confirmation_token'])
    document = read_yaml(path)
    document['memory_policy'] = {}
    document['resources']['db']['policy'] = {}
    path.write_text(write_yaml(document), encoding='utf-8')
    checked = validate_draft(store, 'service')
    assert checked['valid'] and checked['preview']['changes']['changed'] is False
    saved = apply_draft(store, 'service', checked['confirmation_token'])
    assert saved == original
    assert 'memory_policy' not in read_yaml(path)
    assert 'policy' not in read_yaml(path)['resources']['db']
