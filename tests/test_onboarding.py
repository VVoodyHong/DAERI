import copy
import json
import os

import pytest

from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError
from daeri.config.onboarding import create_draft, validate_draft, apply_draft, selection_catalog


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


DB = {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'}


def edit(draft, change):
    from daeri.config.setup_yaml import read_yaml, write_yaml
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
    assert {g['type'] for g in groups} == {'DB', 'Cache', 'Infra', 'Source', 'Document', 'Issue'}
    assert {g['type']: g['label'] for g in groups} == {
        'DB': '데이터베이스', 'Cache': '캐시', 'Infra': '인프라',
        'Source': '소스 코드', 'Document': '문서', 'Issue': '이슈'}
    assert {group['type']: group['instance_prefix'] for group in groups} == {
        kind: kind.lower() for kind in ('DB', 'Cache', 'Infra', 'Source', 'Document', 'Issue')}
    import daeri.config.onboarding as onboarding
    catalog = onboarding.load_catalog()
    monkeypatch.setattr(onboarding, 'load_catalog', lambda: catalog | {
        'db-other': dict(catalog['db-postgres'], display_name='Another DB')})
    db = selection_catalog()['groups'][0]
    assert db['label'] == '데이터베이스'
    assert [p['display_name'] for p in db['products']] == ['PostgreSQL', 'Another DB']
    assert [p['recipe_id'] for p in db['products']] == ['db-postgres', 'db-other']
    assert db['description'] == '사용 가능한 제품: PostgreSQL, Another DB'


def test_draft_contains_only_selected_schema_fields_and_no_service_description(store):
    draft = create_draft(store, 'production', {'primary': 'db-postgres'}, embedding='keyword_only')
    from daeri.config.setup_yaml import read_yaml
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


def test_unnamed_resources_use_category_names_and_repeat_with_stable_suffixes(store):
    from pathlib import Path
    from daeri.config.setup_yaml import read_yaml
    recipes = ['db-postgres', 'db-postgres', 'cache-redis']
    draft = create_draft(store, 'production', recipes=recipes, embedding='keyword_only')
    blocks = read_yaml(draft['path'])['resources']
    assert list(blocks) == ['db', 'db-2', 'cache']
    assert blocks['db']['adapter'] == blocks['db-2']['adapter'] == 'db-postgres'
    edit(draft, lambda value: value['resources']['db-2']['inputs'].update(DB, host='other.example.test'))
    path = Path(draft['path'])
    original = path.read_bytes()
    resumed = create_draft(store, 'production', recipes=recipes)
    assert resumed['resources'] == ['db', 'db-2', 'cache']
    assert path.read_bytes() == original


def test_automatic_names_reserve_role_names_and_skip_another_product(store):
    from daeri.config.setup_yaml import read_yaml
    draft = create_draft(store, 'production', {'db': 'cache-redis', 'db-2': 'db-postgres'},
                         recipes=['db-postgres'], embedding='keyword_only')
    assert list(read_yaml(draft['path'])['resources']) == ['db', 'db-2', 'db-3']
    assert read_yaml(draft['path'])['resources']['db']['adapter'] == 'cache-redis'


@pytest.mark.parametrize('instance', ['db', 'team:db', 'd' * 128])
def test_valid_connection_names_produce_stable_native_credential_references(store, instance):
    from daeri.infrastructure.credentials import Credentials
    from daeri.config.setup import profile_template
    name = 'p' * 64
    draft = create_draft(store, name, {instance: 'db-postgres'}, embedding='keyword_only')
    edit(draft, lambda value: value['resources'][instance]['inputs'].update(DB))
    checked = validate_draft(store, name)
    assert checked['valid'], checked['issues']
    saved = apply_draft(store, name, checked['confirmation_token'])
    reference = saved['resources'][0]['connection']['auth_env']['DATABASE_PASSWORD']
    Credentials()._name(reference)
    repeated = profile_template(store, 'db-postgres', name, instance)['resources'][0]
    assert repeated['connection']['auth_env']['DATABASE_PASSWORD'] == reference
    other = profile_template(store, 'db-postgres', name, instance[:-1] + 'x')['resources'][0]
    assert other['connection']['auth_env']['DATABASE_PASSWORD'] != reference
    if instance == 'db':
        assert reference == f'credential:{name}.db.database_password'


def test_automatic_names_reuse_saved_default_connections_and_preserve_legacy_names(store):
    from daeri.config.setup import build_setup_resource
    from daeri.config.setup_yaml import read_yaml
    resources = [build_setup_resource(store, 'db-postgres', 'production', name, DB)
                 for name in ('db', 'legacy-postgres')]
    store.put_profile('production', resources=resources, embedding={'provider': 'keyword_only'})
    draft = create_draft(store, 'production', recipes=['db-postgres', 'db-postgres'])
    assert list(read_yaml(draft['path'])['resources']) == ['db', 'db-2']
    assert read_yaml(draft['path'])['resources']['db']['inputs'] == DB
    edit(draft, lambda value: value['resources']['db-2']['inputs'].update(DB, host='other.example.test'))
    result = validate_draft(store, 'production')
    saved = apply_draft(store, 'production', result['confirmation_token'])
    assert {resource['instance_id'] for resource in saved['resources']} == {'db', 'db-2', 'legacy-postgres'}


def test_another_recipe_for_the_same_adapter_gets_a_new_default_slot(store, monkeypatch):
    import daeri.config.onboarding as onboarding
    catalog = onboarding.load_catalog()
    monkeypatch.setattr(onboarding, 'load_catalog', lambda: catalog | {
        'db-alternative': dict(catalog['db-postgres'])})
    selected = onboarding._automatic_resources(store, 'production', {}, ['db-alternative'],
        {'db': {'adapter': 'db-postgres', 'inputs': DB}}, None)
    assert selected == {'db-2': 'db-alternative'}


def test_automatic_names_preserve_manual_connections_outside_the_selected_recipe(store):
    import sys
    from daeri.config.setup import build_setup_resource
    resource = build_setup_resource(store, 'db-postgres', 'production', 'db', DB)
    resource['connection']['command'] = sys.executable
    resource['connection']['args'] = []
    store.put_profile('production', resources=[resource], embedding={'provider': 'keyword_only'})
    draft = create_draft(store, 'production', recipes=['db-postgres'])
    assert draft['resources'] == ['db-2']
    assert store.get_profile('production')['resources'] == [resource]


@pytest.mark.parametrize('recipes', ['db-postgres', [None], [[]], ['unregistered']])
def test_invalid_automatic_resource_requests_preserve_the_draft(store, recipes):
    from pathlib import Path
    draft = completed(store)
    original = Path(draft['path']).read_bytes()
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production', recipes=recipes)
    assert Path(draft['path']).read_bytes() == original


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
    assert all(r['policy'] is None for r in profile['resources'])
    again = validate_draft(store, 'production')
    assert apply_draft(store, 'production', again['confirmation_token'])['revision'] == 1


def test_all_current_resource_categories_share_the_same_yaml_apply_flow(store):
    from daeri.config.setup import load_catalog
    selections = {recipe: recipe for recipe in load_catalog()}
    draft = create_draft(store, 'production', selections, embedding='keyword_only')
    answers = {
        'db-postgres': DB,
        'cache-redis': {'host': 'cache.example.test'},
        'infra-aws': {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': '111111111111'},
        'source-gitlab': {'server': 'https://git.example.test'},
        'document-confluence': {'server': 'https://docs.example.test/wiki', 'username': 'reader@example.test'},
        'issue-jira': {'server': 'https://issues.example.test', 'auth_type': 'api_token', 'username': 'reader@example.test'},
    }
    edit(draft, lambda value: [value['resources'][name]['inputs'].update(fields)
                               for name, fields in answers.items()])
    result = validate_draft(store, 'production')
    assert result['valid'], result['issues']
    profile = apply_draft(store, 'production', result['confirmation_token'])
    assert profile['revision'] == 1 and len(profile['resources']) == len(selections)
    assert {r['type'] for r in profile['resources']} == {'DB', 'Cache', 'Infra', 'Source', 'Document', 'Issue'}
    assert all(r['policy'] is None for r in profile['resources'])


def test_multiple_jira_domains_apply_separate_connection_only_yaml_blocks(store):
    from daeri.config.setup_yaml import read_yaml
    draft = create_draft(store, 'production', recipes=['issue-jira', 'issue-jira'], embedding='keyword_only')
    blocks = read_yaml(draft['path'])['resources']
    assert list(blocks) == ['issue', 'issue-2']
    assert all(set(block['inputs']) == {'server', 'auth_type', 'username'} for block in blocks.values())
    def connect(value):
        value['resources']['issue']['inputs'].update(server='https://team-a.atlassian.net',
                                                    username='reader@example.test')
        value['resources']['issue-2']['inputs'].update(server='https://issues.team-b.example.test',
                                                      auth_type='personal_token')
    edit(draft, connect)
    checked = validate_draft(store, 'production')
    assert checked['valid'], checked['issues']
    saved = apply_draft(store, 'production', checked['confirmation_token'])
    first, second = saved['resources']
    assert (first['instance_id'], second['instance_id']) == ('issue', 'issue-2')
    assert first['target']['server'] == 'https://team-a.atlassian.net'
    assert second['target']['server'] == 'https://issues.team-b.example.test'
    assert first['connection']['auth_env'] == {'JIRA_API_TOKEN': 'credential:production.issue.jira_api_token'}
    assert second['connection']['auth_env'] == {'JIRA_PERSONAL_TOKEN': 'credential:production.issue-2.jira_personal_token'}
    assert first['identity_ref'] != second['identity_ref']
    assert first['policy'] is None and second['policy'] is None


@pytest.mark.parametrize('account,identifier', [
    ('123456789012', '123456789012'), ('012345678901', '012345678901'),
])
def test_unquoted_infra_account_yaml_is_previewed_and_saved_as_a_string(store, account, identifier):
    from pathlib import Path
    draft = create_draft(store, 'production', {'infra': 'infra-aws'}, embedding='keyword_only')
    path = Path(draft['path'])
    text = path.read_text(encoding='utf-8')
    text = text.replace('profile: null', 'profile: operations-reader')
    text = text.replace('region: null', 'region: ap-northeast-2').replace('account: null', 'account: ' + account)
    path.write_text(text, encoding='utf-8')
    result = validate_draft(store, 'production')
    assert result['valid'], result['issues']
    assert result['preview']['resources']['infra']['inputs']['account'] == identifier
    profile = apply_draft(store, 'production', result['confirmation_token'])
    assert profile['resources'][0]['target']['account'] == identifier
    assert path.read_text(encoding='utf-8') == text


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


def test_setup_previews_generated_read_contract_refresh_before_apply(store, monkeypatch):
    import daeri.config.onboarding as onboarding
    from daeri.config.setup import build_setup_resource
    resource = build_setup_resource(store, 'db-postgres', 'production', 'primary', DB)
    resource.pop('discover_tools')
    old_constraints = {'sql_field': 'sql', 'tables': ['*'], 'columns': ['*'], 'max_rows': 100}
    resource['capabilities'][0]['constraints'] = old_constraints
    profile = store.put_profile('production', resources=[resource], embedding={'provider': 'keyword_only'})
    create_draft(store, 'production', {'primary': 'db-postgres'})

    checked = validate_draft(store, 'production')
    assert checked['valid'], checked['issues']
    assert checked['preview']['changes']['resource_contracts'] == {'primary': {
        'before': {'discover_tools': False, 'capabilities': resource['capabilities'], 'enabled_tools': None},
        'after': {'discover_tools': True, 'capabilities': [
            {'capability_id': 'query', 'tool_name': 'execute_sql', 'constraints': {}}], 'enabled_tools': None}}}
    assert store.get_profile('production') == profile

    def changed_contract(*args, **kwargs):
        changed = build_setup_resource(*args, **kwargs)
        changed['discover_tools'] = False
        return changed

    with monkeypatch.context() as patch:
        patch.setattr(onboarding, 'build_setup_resource', changed_contract)
        with pytest.raises(StoreError, match='REVISION_CONFLICT'):
            apply_draft(store, 'production', checked['confirmation_token'])
    assert store.get_profile('production') == profile
    saved = apply_draft(store, 'production', checked['confirmation_token'])
    assert saved['resources'][0]['discover_tools'] is True
    assert saved['resources'][0]['capabilities'][0]['constraints'] == {}


def test_setup_contract_preview_includes_removed_lower_tool_filter(store):
    from daeri.config.setup import build_setup_resource
    resource = build_setup_resource(store, 'issue-jira', 'production', 'issues', {
        'server': 'https://issues.example.test', 'auth_type': 'personal_token'})
    resource.pop('discover_tools')
    selected = ','.join(cap['tool_name'] for cap in resource['capabilities'])
    resource['connection']['env']['ENABLED_TOOLS'] = selected
    store.put_profile('production', resources=[resource], embedding={'provider': 'keyword_only'})
    create_draft(store, 'production', {'issues': 'issue-jira'})

    checked = validate_draft(store, 'production')
    assert checked['valid'], checked['issues']
    change = checked['preview']['changes']['resource_contracts']['issues']
    assert change['before']['enabled_tools'] == selected
    assert change['after']['enabled_tools'] is None
    assert change['before']['discover_tools'] is False
    assert change['after']['discover_tools'] is True


def test_setup_contract_preview_preserves_administrator_read_restrictions(store):
    from daeri.config.setup import build_setup_resource
    resource = build_setup_resource(store, 'cache-redis', 'production', 'cache', {
        'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True})
    resource.pop('discover_tools')
    resource['capabilities'][0]['constraints'] = {'fixed': {'key': 'only-approved'}}
    profile = store.put_profile('production', resources=[resource], embedding={'provider': 'keyword_only'})
    create_draft(store, 'production', {'cache': 'cache-redis'})

    checked = validate_draft(store, 'production')
    assert checked['valid'], checked['issues']
    assert checked['preview']['changes']['resource_contracts'] == {}
    assert checked['preview']['changes']['updated_resources'] == []
    saved = apply_draft(store, 'production', checked['confirmation_token'])
    assert saved == profile
    assert saved['resources'][0]['capabilities'] == resource['capabilities']
    assert not saved['resources'][0].get('discover_tools', False)


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
    from daeri.config.setup_yaml import read_yaml
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
    lambda v: v.update(retention_days=10 ** 100),
    lambda v: v.update(long_term_memory_days=10 ** 100),
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
    from daeri.config.setup_yaml import read_yaml
    value = read_yaml(draft['path'])
    assert value['embedding'] == {'provider': 'api', 'adapter': 'openai_compatible', 'endpoint': None,
        'model': None, 'credential_ref': 'credential:production.embedding',
        'credentials': {'api_key': None}}
    edit(draft, lambda v: v['embedding'].update(endpoint='https://embedding.example.test/v1/embeddings', model='company-model'))
    result = validate_draft(store, 'production')
    assert result['valid']
    profile = apply_draft(store, 'production', result['confirmation_token'])
    assert profile['embedding']['provider'] == 'api'
    assert profile['embedding']['adapter'] == 'openai_compatible'


def test_api_adapter_catalog_and_selected_template_share_the_registered_contract(store):
    from daeri.config.setup_yaml import read_yaml
    adapters = selection_catalog()['embedding_adapters']
    assert [adapter['adapter_id'] for adapter in adapters] == ['openai_compatible']
    assert all(adapter['label'] and adapter['description'] for adapter in adapters)
    draft = create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    embedding = read_yaml(draft['path'])['embedding']
    assert embedding['provider'] == 'api' and embedding['adapter'] == 'openai_compatible'
    assert {'endpoint', 'model', 'credential_ref'} <= embedding.keys()
    assert 'allow_remote' not in embedding


@pytest.mark.parametrize('provider', ['local', 'keyword_only', 'api'])
def test_embedding_draft_comments_explain_the_selected_fields_without_changing_values(store, provider):
    from pathlib import Path
    from daeri.config.setup_yaml import parse_yaml
    draft = create_draft(store, 'production', embedding=provider)
    text = Path(draft['path']).read_text(encoding='utf-8')
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith('  ') and not line.startswith('    ') and not line.lstrip().startswith('#'):
            assert lines[index - 1].startswith('  # ')
    assert parse_yaml(text.encode())['embedding'] == (
        {'provider': 'api', 'adapter': None} if provider == 'api' else {'provider': provider})


def test_api_embedding_comments_cover_connection_authentication_and_optional_fields(store):
    from pathlib import Path
    from daeri.config.setup_yaml import parse_yaml
    config = {'provider': 'api', 'adapter': 'openai_compatible',
        'endpoint': 'https://embedding.example.test/v1/embeddings', 'model': 'company-model',
        'credential_ref': 'env:COMPANY_EMBEDDING_TOKEN',
        'dimensions': 1024, 'timeout_seconds': 30}
    store.put_profile('production', embedding=config)
    draft = create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    text = Path(draft['path']).read_text(encoding='utf-8')
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith('  ') and not line.startswith('    ') and not line.lstrip().startswith('#'):
            assert lines[index - 1].startswith('  # '), line
    assert 'HTTPS' in text and '모델' in text
    assert '인증 참조' in text and 'allow_remote' not in text
    assert '1~8192' in text and '1~60' in text
    assert parse_yaml(text.encode())['embedding'] == {
        **config, 'credentials': {'api_key': {'ref': config['credential_ref']}}}
    assert validate_draft(store, 'production')['valid']


def test_registered_api_template_uses_its_own_comments_and_nested_fields(store, monkeypatch):
    from pathlib import Path
    from daeri.infrastructure.embedding import API_ADAPTERS
    from daeri.config.setup_yaml import read_yaml

    class CompanyFormat:
        field_comments = {'transport': '사내 API의 접속 방식입니다.'}

        @staticmethod
        def draft_config(name):
            return {'provider': 'api', 'adapter': 'company_format',
                    'transport': {'adapter': 'internal'}, 'allow_remote': False}

    monkeypatch.setitem(API_ADAPTERS, 'company_format', CompanyFormat)
    draft = create_draft(store, 'production', {'primary': 'db-postgres'},
                         embedding='api', embedding_adapter='company_format')
    text = Path(draft['path']).read_text(encoding='utf-8')
    assert '# 사내 API의 접속 방식입니다.\n  transport:' in text
    assert read_yaml(draft['path'])['embedding'] == {
        key: value for key, value in CompanyFormat.draft_config('production').items() if key != 'allow_remote'}
    assert 'endpoint:' not in text and 'OpenAI' not in text


def test_company_api_without_selected_adapter_stays_incomplete(store):
    from daeri.config.setup_yaml import read_yaml
    draft = create_draft(store, 'production', embedding='api')
    assert read_yaml(draft['path'])['embedding'] == {'provider': 'api', 'adapter': None}
    result = validate_draft(store, 'production')
    assert not result['complete'] and result['confirmation_token'] is None
    assert [issue['field'] for issue in result['issues']] == ['embedding.adapter']
    with pytest.raises(StoreError):
        apply_draft(store, 'production', result['confirmation_token'])
    assert store.list_profiles() == []


@pytest.mark.parametrize('adapter', [None, 'COMPANY_PRIVATE_SENTINEL', [], {}])
def test_unsupported_yaml_api_adapter_reports_the_selection_field_without_raw_values(store, adapter):
    draft = create_draft(store, 'production', embedding='api')
    edit(draft, lambda value: value['embedding'].update(adapter=adapter))
    result = validate_draft(store, 'production')
    assert not result['complete'] and result['confirmation_token'] is None
    assert [issue['field'] for issue in result['issues']] == ['embedding.adapter']
    assert 'COMPANY_PRIVATE_SENTINEL' not in json.dumps(result)


def test_missing_yaml_api_adapter_reports_the_selection_field(store):
    draft = create_draft(store, 'production', embedding='api')
    edit(draft, lambda value: value['embedding'].pop('adapter'))
    result = validate_draft(store, 'production')
    assert [issue['field'] for issue in result['issues']] == ['embedding.adapter']


def test_supported_api_adapter_with_incomplete_fields_reports_embedding(store):
    create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    result = validate_draft(store, 'production')
    assert [issue['field'] for issue in result['issues']] == ['embedding']


def test_explicit_adapter_selection_completes_a_pending_api_template(store):
    from daeri.config.setup_yaml import read_yaml
    draft = create_draft(store, 'production', embedding='api')
    create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    embedding = read_yaml(draft['path'])['embedding']
    assert embedding['adapter'] == 'openai_compatible'
    assert {'endpoint', 'model', 'credential_ref'} <= embedding.keys()
    assert 'allow_remote' not in embedding


def test_pending_api_template_with_user_edits_uses_the_yaml_edit_path(store):
    from pathlib import Path
    draft = create_draft(store, 'production', embedding='api')
    edit(draft, lambda value: value['embedding'].update(model='company-model'))
    original = Path(draft['path']).read_bytes()
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    assert Path(draft['path']).read_bytes() == original


@pytest.mark.parametrize('provider,adapter', [
    ('api', 'unregistered'), ('local', 'openai_compatible'),
    ('keyword_only', 'openai_compatible'), (None, 'openai_compatible'),
    ('openai_compatible', 'unregistered'),
])
def test_invalid_api_adapter_selection_preserves_existing_draft(store, provider, adapter):
    from pathlib import Path
    draft = completed(store)
    original = Path(draft['path']).read_bytes()
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        create_draft(store, 'production', embedding=provider, embedding_adapter=adapter)
    assert Path(draft['path']).read_bytes() == original


@pytest.mark.parametrize('provider,adapter', [('openai_compatible', None), ('api', 'openai_compatible')])
def test_reopening_legacy_api_draft_preserves_user_edited_bytes(store, provider, adapter):
    from pathlib import Path
    draft = create_draft(store, 'production', embedding='openai_compatible')
    edit(draft, lambda value: value['embedding'].update(provider='openai_compatible',
        endpoint='https://embedding.example.test/v1/embeddings', model='company-model'))
    edit(draft, lambda value: value['embedding'].pop('adapter', None))
    path = Path(draft['path'])
    path.write_text('# User retained this explanation.\n' + path.read_text(), encoding='utf-8')
    original = path.read_bytes()
    create_draft(store, 'production', embedding=provider, embedding_adapter=adapter)
    assert path.read_bytes() == original


def test_new_draft_reuses_existing_api_settings_for_the_same_adapter(store):
    from daeri.config.setup_yaml import read_yaml
    profile = store.put_profile('production', embedding={'provider': 'openai_compatible',
        'endpoint': 'https://embedding.example.test/v1/embeddings', 'model': 'company-model',
        'credential_ref': 'credential:production.embedding'})
    draft = create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    assert read_yaml(draft['path'])['embedding'] == {
        **profile['embedding'], 'credentials': {'api_key': {'ref': profile['embedding']['credential_ref']}}}


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
    from daeri.config.setup import preview_profile_deletion, delete_profile
    outside = tmp_path / 'backup.yaml'
    outside.write_bytes(Path(draft['path']).read_bytes())
    class Credentials:
        def delete_if_present(self, ref): pass
    delete_profile(store, 'production', preview_profile_deletion(store, 'production')['deletion_token'], credentials=Credentials())
    assert not Path(draft['path']).exists()
    assert not (store.data_dir / 'setup' / 'production').exists()
    assert outside.exists()
