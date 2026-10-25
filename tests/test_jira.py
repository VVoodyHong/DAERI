"""Jira tenant isolation, authentication modes, and approved read arguments."""

import copy
import json
import re
import sys
from types import SimpleNamespace

import pytest

from daeri.adapters import get_adapter
from daeri.resources import validate_resource
from daeri.setup import build_setup_resource, load_catalog, profile_template, setup_schema
from daeri.storage import Store, StoreError


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'private')
    yield result
    result.close()


def configured(store, *, instance='issue', server='https://team.atlassian.net', auth_type='api_token', username='reader@example.test'):
    return build_setup_resource(store, 'issue-jira', 'service', instance,
                                {'server': server, 'auth_type': auth_type, 'username': username})


def test_jira_is_a_separate_issue_category_with_a_pinned_read_only_recipe(store):
    item = load_catalog()['issue-jira']
    assert (item['type'], item['adapter'], item['package'], item['version']) == ('Issue', 'jira_read', 'mcp-atlassian', '0.23.1')
    config = configured(store)
    assert validate_resource(config) == config
    assert config['connection']['env']['READ_ONLY_MODE'] == 'true'
    assert config['connection']['env']['JIRA_SSL_VERIFY'] == 'true'
    assert {c['tool_name'] for c in config['capabilities']} == {
        'jira_get_issue', 'jira_get_project_issues', 'jira_get_transitions', 'jira_search'}
    assert config['capabilities'][0]['constraints']['fixed']['update_history'] is False
    assert set(setup_schema('issue-jira')['input_schema']['properties']) == {'server', 'auth_type', 'username'}


def test_different_jira_domains_keep_independent_credentials_and_targets(store):
    first = configured(store, instance='issue', server='https://one.atlassian.net')
    second = configured(store, instance='issue-2', server='https://two.atlassian.net')
    assert first['target']['server'] != second['target']['server']
    assert first['identity_ref'] != second['identity_ref']
    assert set(first['connection']['auth_env'].values()).isdisjoint(second['connection']['auth_env'].values())
    for item in (first, second):
        assert get_adapter('jira_read').binding_files(item, {}) == {}
    second['connection']['env']['JIRA_URL'] = first['target']['server']
    with pytest.raises(StoreError, match='CONTEXT_CHANGED'):
        get_adapter('jira_read').binding_files(second, {})


def test_cloud_uses_username_and_api_token_and_server_pat_has_its_own_reference(store):
    cloud = configured(store)
    assert set(cloud['connection']['auth_env']) == {'JIRA_API_TOKEN'}
    server = configured(store, server='https://jira.example.test/jira', auth_type='personal_token', username='')
    assert set(server['connection']['auth_env']) == {'JIRA_PERSONAL_TOKEN'}
    assert '.issue.jira_personal_token' in server['connection']['auth_env']['JIRA_PERSONAL_TOKEN']
    assert validate_resource(server) == server
    launched = get_adapter('jira_read').launch_connection(server)
    assert 'DAERI_JIRA_AUTH_TYPE' not in launched['env']
    assert launched['auth_env'] == server['connection']['auth_env']
    assert 'JIRA_USERNAME' not in launched['env']


@pytest.mark.parametrize('server,auth_type,username', [
    ('https://team.atlassian.net', 'personal_token', ''),
    ('https://team.atlassian.net', 'api_token', ''),
    ('https://jira.example.test', 'api_token', ''),
])
def test_incomplete_or_unsupported_authentication_is_rejected(store, server, auth_type, username):
    with pytest.raises(StoreError):
        validate_resource(configured(store, server=server, auth_type=auth_type, username=username))


@pytest.mark.parametrize('field,value', [
    ('ATLASSIAN_OAUTH_ENABLE', 'true'), ('ATLASSIAN_OAUTH_CLOUD_ID', 'other'),
    ('JIRA_CUSTOM_HEADERS', 'Authorization=alternative'), ('JIRA_HTTP_PROXY', 'https://proxy.example.test'),
    ('JIRA_SSL_VERIFY', 'false'), ('JIRA_CLIENT_CERT', '/other/cert.pem'),
    ('CONFLUENCE_URL', 'https://other.atlassian.net/wiki'), ('JIRA_URL', 'http://jira.example.test'),
    ('READ_ONLY_MODE', 'false'), ('ENABLED_TOOLS', 'jira_create_issue'),
])
def test_alternate_tenants_authentication_and_mutations_fail_before_launch(store, field, value):
    resource = configured(store)
    resource['connection']['env'][field] = value
    with pytest.raises(StoreError):
        validate_resource(resource)


def test_api_authentication_cannot_be_ambiguously_combined_with_a_second_token(store):
    resource = configured(store)
    resource['connection']['auth_env']['JIRA_PERSONAL_TOKEN'] = 'credential:service.other.token'
    with pytest.raises(StoreError):
        validate_resource(resource)


@pytest.mark.parametrize('capabilities', [
    None, {}, [{}], [None],
    [{'capability_id': 'read', 'tool_name': [], 'constraints': {}}],
    [{'capability_id': 'read', 'tool_name': {}, 'constraints': {}}],
])
def test_malformed_capabilities_return_an_input_error_before_adapter_checks(store, capabilities):
    resource = configured(store)
    resource['capabilities'] = capabilities
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate_resource(resource)


@pytest.mark.parametrize('parameters', [
    {'issue_key': 'OPS-42', 'update_history': False},
    {'project_key': 'OPS', 'limit': 25, 'start_at': 0},
    {'jql': 'project = OPS AND status = Open ORDER BY updated DESC', 'limit': 20},
])
def test_explicitly_approved_issue_project_and_jql_reads_are_available(parameters):
    assert get_adapter('jira_read').normalize(parameters, {}, {'equals': copy.deepcopy(parameters)})['fields'] == sorted(parameters)


@pytest.mark.parametrize('parameters', [
    {'issue_key': 'https://other.atlassian.net/browse/OPS-1'},
    {'issue_key': '../OPS-1'}, {'project_key': 'OPS OR project=OTHER'},
    {'url': 'https://other.atlassian.net'}, {'issue_key': 'OPS-42', 'update_history': True},
])
def test_indirect_urls_invalid_identifiers_and_history_updates_are_rejected(parameters):
    with pytest.raises(StoreError):
        get_adapter('jira_read').normalize(parameters, {}, {'equals': copy.deepcopy(parameters)})


def test_get_issue_requires_history_updates_disabled_even_when_provider_default_is_true():
    parameters = {'issue_key': 'OPS-42', 'update_history': True}
    get_adapter('jira_read').prepare_parameters(parameters, {'properties': {'update_history': {'default': True}}})
    assert parameters['update_history'] is False


def test_unapproved_search_change_is_rejected():
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        get_adapter('jira_read').normalize({'jql': 'project = OTHER'}, {}, {'equals': {'jql': 'project = OPS'}})


@pytest.mark.parametrize('issues,total,expected', [
    ([], 0, None),
    ([{'key': 'OPS-1'}], 1, None),
    ([{'key': 'OPS-1'}], 2, None),
    ([{'key': 'OPS-1'}], 0, None),
])
def test_collection_completeness_is_left_to_the_host(issues, total, expected):
    invocation = SimpleNamespace(operation='jira_search')
    result = {'data': {'issues': issues, 'start_at': 0, 'total': total}}
    assert get_adapter('jira_read').source_complete(result, invocation) is expected


def test_changing_authentication_mode_preserves_a_custom_credential_reference(store):
    original = configured(store, server='https://jira.example.test')
    original['connection']['auth_env'] = {'JIRA_API_TOKEN': 'credential:company.jira-reader'}
    changed = build_setup_resource(store, 'issue-jira', 'service', 'issue',
                                   {'auth_type': 'personal_token', 'username': ''}, existing=original)
    assert changed['connection']['auth_env'] == {'JIRA_PERSONAL_TOKEN': 'credential:company.jira-reader'}
    assert original['connection']['auth_env'] == {'JIRA_API_TOKEN': 'credential:company.jira-reader'}
    restored = build_setup_resource(store, 'issue-jira', 'service', 'issue',
                                    {'auth_type': 'api_token', 'username': 'reader@example.test'}, existing=changed)
    assert restored['connection']['auth_env'] == original['connection']['auth_env']


@pytest.mark.parametrize('profile,instance', [
    ('service', 'team:issue'), ('service', 'i' * 128), ('p' * 64, 'i' * 128),
])
def test_generated_token_references_support_legal_instance_ids_and_authentication_switches(store, profile, instance):
    original = build_setup_resource(store, 'issue-jira', profile, instance,
                                    {'server': 'https://jira.example.test', 'auth_type': 'api_token', 'username': 'reader@example.test'})
    changed = build_setup_resource(store, 'issue-jira', profile, instance,
                                   {'auth_type': 'personal_token', 'username': ''}, existing=original)
    restored = build_setup_resource(store, 'issue-jira', profile, instance,
                                    {'auth_type': 'api_token', 'username': 'reader@example.test'}, existing=changed)
    first = original['connection']['auth_env']['JIRA_API_TOKEN']
    second = changed['connection']['auth_env']['JIRA_PERSONAL_TOKEN']
    assert all(re.fullmatch(r'credential:[A-Za-z0-9._-]{1,128}', reference) for reference in (first, second))
    assert first != second
    assert restored['connection']['auth_env'] == original['connection']['auth_env']
    assert all(validate_resource(resource) == resource for resource in (original, changed, restored))


def test_changing_public_connection_values_preserves_existing_token_reference(store):
    original = configured(store)
    original['connection']['auth_env'] = {'JIRA_API_TOKEN': 'credential:company.jira-reader'}
    changed = build_setup_resource(store, 'issue-jira', 'service', 'issue',
                                   {'server': 'https://updated.atlassian.net'}, existing=original)
    assert changed['connection']['auth_env'] == original['connection']['auth_env']
    assert changed['target']['server'] == 'https://updated.atlassian.net'


def test_jira_stdio_discovery_normalization_and_execution_use_the_selected_tenant(store, tmp_path):
    from daeri.resources import ResourceRegistry
    script = tmp_path / 'jira_mcp.py'
    script.write_text('''import os,json
from mcp.server.fastmcp import FastMCP
server = FastMCP("jira-contract-fixture")
@server.tool(name="jira_get_issue")
def issue(issue_key: str, update_history: bool=True) -> str:
    assert update_history is False
    return json.dumps({"key": issue_key, "server": os.environ["JIRA_URL"], "history_updated": update_history})
@server.tool(name="jira_get_project_issues")
def project(project_key: str, limit: int=10, start_at: int=0) -> str:
    return json.dumps({"issues": [{"key":project_key+"-1"}],"start_at":start_at,"total":2})
@server.tool(name="jira_get_transitions")
def transitions(issue_key: str) -> str:
    return "[]"
@server.tool(name="jira_search")
def search(jql: str, limit: int=10) -> str:
    return json.dumps({"issues": [],"start_at":0,"total":0})
server.run()
''')
    resources = [configured(store, instance='issue', server='https://one.atlassian.net'),
                 configured(store, instance='issue-2', server='https://two.atlassian.net')]
    for resource in resources:
        resource['connection']['command'] = sys.executable
        resource['connection']['args'] = ['-I', str(script)]
    profile = store.put_profile('service', resources=resources)
    registry = ResourceRegistry(store, credential_resolver=lambda reference: 'private-' + reference)
    for resource in resources:
        instance = resource['instance_id']
        assert registry.diagnose(profile, instance)['capability_count'] == 4
        request = registry.normalize(profile, instance, 'read-issue', {'issue_key': 'OPS-42'},
                                     {'equals': {'issue_key': 'OPS-42', 'update_history': False}})
        result = registry.execute(request)
        assert result['result']['data'] == {'key': 'OPS-42', 'server': resource['target']['server'], 'history_updated': False}
    page = registry.normalize(profile, 'issue', 'list-project-issues', {'project_key': 'OPS'},
                              {'equals': {'project_key': 'OPS'}, 'max_values': {'limit': 50, 'start_at': 100}})
    assert registry.execute(page)['source_complete'] is None
    search = registry.normalize(profile, 'issue-2', 'search-issues', {'jql': 'project = OPS'},
                                {'equals': {'jql': 'project = OPS'}, 'max_values': {'limit': 50}})
    assert registry.execute(search)['source_complete'] is None
