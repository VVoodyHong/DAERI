"""Reuse lower MCP reads while keeping connection, explicit policy and contract boundaries."""

import copy
import json
import sys

import pytest
from mcp.types import CallToolResult, EmbeddedResource, BlobResourceContents, ImageContent, TextContent

from daeri.infrastructure.adapters import get_adapter
from daeri.infrastructure.resources import ResourceRegistry, validate_resource
from daeri.config.setup import build_setup_resource
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


@pytest.mark.parametrize('adapter,parameters', [
    ('postgres_read', {'sql': "WITH totals AS (SELECT date_trunc('day', created_at) AS day, sum(amount) AS n FROM public.orders GROUP BY 1) SELECT * FROM totals JOIN public.calendar USING (day)"}),
    ('redis_read', {'pattern': 'session:*', 'cursor': 0, 'count': 100}),
    ('redis_read', {'name': 'user:*', 'key': 'rating[2025]'}),
    ('confluence_read', {'query': 'space = OPS AND text ~ "outage"', 'limit': 25}),
    ('jira_read', {'jql': 'project = OPS AND status = Open', 'start_at': 50, 'limit': 25}),
])
def test_host_selects_query_filters_and_pages_without_preapproving_every_argument(adapter, parameters):
    original = copy.deepcopy(parameters)
    assert get_adapter(adapter).normalize(parameters, {}, {}) == {'fields': sorted(parameters)}
    assert parameters == original


def test_explicit_field_restrictions_still_apply_to_free_queries():
    adapter = get_adapter('jira_read')
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        adapter.normalize({'jql': 'project = OTHER'}, {}, {'equals': {'jql': 'project = OPS'}})
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        get_adapter('redis_read').normalize({'key': 'other'}, {'fixed': {'key': 'approved'}}, {})


@pytest.mark.parametrize('guards', [
    {'allowed_values': {'ref': ['main']}},
    {'path_prefixes': {'path': ['src']}},
    {'max_values': {'limit': 10}},
])
def test_explicit_constraints_require_values_instead_of_unknown_provider_defaults(guards):
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        get_adapter('confluence_read').normalize({'project_id': 'team/service'}, guards, {})


@pytest.mark.parametrize('policy', [
    {'tables': ['public.orders'], 'columns': ['id'], 'max_rows': 100},
    {'tables': ['*'], 'columns': ['*'], 'max_rows': 100},
])
@pytest.mark.parametrize('adapter,parameters', [
    ('postgres_read', {'sql': 'SELECT * FROM private.users'}),
    ('redis_read', {'key': 'private:users'}),
    ('confluence_read', {'page_id': '123'}),
    ('jira_read', {'jql': 'project = PRIVATE'}),
    ('aws_read', {'cli_command': 'aws ec2 describe-instances'}),
])
def test_unenforceable_registered_restrictions_are_never_silently_ignored(policy, adapter, parameters):
    if adapter == 'aws_read':
        policy = {**policy, 'region': 'ap-northeast-2'}
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        get_adapter(adapter).normalize(parameters, policy, {})


def test_plan_rejects_scope_conditions_the_read_adapter_cannot_enforce(tmp_path):
    from daeri.application.runtime import Runtime
    from test_execution_flow import resource

    runtime = Runtime(tmp_path / 'private')
    try:
        profile = runtime.store.put_profile('service', resources=[resource()])
        runtime.registry.diagnose(profile, 'cache-a')
        task = runtime.call('daeri_task_create', {
            'request_text': 'Read public columns only',
            'context': {'profile_id': profile['profile_id']},
        })['data']
        result = runtime.call('daeri_plan_submit', {
            'task_id': task['task_id'],
            'draft': {
                'purpose': 'Read public columns only',
                'steps': [{'step_id': 'read', 'description': 'Read the approved data',
                           'instance_id': 'cache-a', 'capability_id': 'get',
                           'scope': {'tables': ['public.orders'], 'columns': ['public_column'], 'max_rows': 1}}],
                'output': {'format': 'text'},
                'completion_criteria': [{'criterion_id': 'read', 'description': 'Read the approved data',
                                         'step_ids': ['read']}],
            },
        })
        assert result['status'] == 'error', result
        assert result['error']['code'] == 'PLAN_INVALID'
        assert runtime.store.get_task(task['task_id'])['plans'] == []
    finally:
        runtime.close()


def test_aws_reuses_lower_read_policy_outside_the_old_ten_operations():
    arguments = {'cli_command': 'aws logs filter-log-events --log-group-name prod --region ap-northeast-2', 'max_results': 25}
    access = get_adapter('aws_read').normalize(arguments, {'region': 'ap-northeast-2'}, {})
    assert access['operation'] == 'filter-log-events'
    assert arguments['max_results'] == 25


def test_aws_batch_queries_keep_each_commands_bound_region():
    arguments = {'cli_command': ['aws ecs list-clusters --region ap-northeast-2',
                                 'aws lambda list-functions --region ap-northeast-2']}
    access = get_adapter('aws_read').normalize(arguments, {'region': 'ap-northeast-2'}, {})
    assert [item['operation'] for item in access['commands']] == ['list-clusters', 'list-functions']
    arguments['cli_command'].append('aws ecs list-clusters --region us-east-1')
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        get_adapter('aws_read').normalize(arguments, {'region': 'ap-northeast-2'}, {})


@pytest.mark.parametrize('suffix', ['', ' --region=ap-northeast-2', ' --region ap-northeast-2 --query clusters --output json'])
def test_aws_bound_region_default_and_output_selection_are_not_separately_restricted(suffix):
    arguments = {'cli_command': 'aws ecs list-clusters' + suffix}
    assert get_adapter('aws_read').normalize(arguments, {'region': 'ap-northeast-2'}, {})['region'] == 'ap-northeast-2'


def test_aws_configured_command_region_cannot_disagree_with_connection_target(registry):
    resource = build_setup_resource(registry.store, 'infra-aws', 'service', 'aws',
                                    {'profile': 'reader', 'region': 'ap-northeast-2', 'account': '111111111111'})
    resource['capabilities'][0]['constraints']['region'] = 'us-east-1'
    with pytest.raises(StoreError, match='CONTEXT_CHANGED'):
        validate_resource(resource)


def test_document_optional_arguments_remain_unspecified_within_broad_scope():
    adapter = get_adapter('confluence_read')
    arguments = {'query': 'space = OPS'}
    adapter.prepare_parameters(arguments, {'properties': {'query': {'type': 'string'}, 'limit': {'default': None}}})
    assert adapter.normalize(arguments, {}, {}) == {'fields': ['query']}
    assert arguments == {'query': 'space = OPS'}


def test_a_lower_tool_explicitly_marked_as_mutating_is_not_registered_as_read():
    assert not get_adapter('confluence_read').is_read_tool({'name': 'confluence_get_page',
                                                       'annotations': {'readOnlyHint': False}})


@pytest.mark.parametrize('command', [
    'aws logs filter-log-events --region ap-northeast-2 --profile other',
    'aws logs filter-log-events --region ap-northeast-2 --endpoint-url=https://other.test',
    'aws logs filter-log-events --region us-east-1',
    'aws logs filter-log-events --region ap-northeast-2 --filters file://private.json',
])
def test_aws_freedom_does_not_change_connection_or_read_local_files(command):
    with pytest.raises(StoreError):
        get_adapter('aws_read').normalize({'cli_command': command}, {'region': 'ap-northeast-2'}, {})


@pytest.fixture
def registry(tmp_path):
    store = Store(tmp_path / 'private')
    yield ResourceRegistry(store, credential_resolver=lambda ref: 'private-test-token')
    store.close()


def lower_fixture(tmp_path):
    script = tmp_path / 'read_tools.py'
    script.write_text('''import asyncio,json,os
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool,ToolAnnotations,CallToolResult,TextContent
server=Server("autonomous-reads")
@server.list_tools()
async def tools():
    return [Tool(name=name, inputSchema={"type":"object","additionalProperties":True},
                 annotations=ToolAnnotations(readOnlyHint=read)) for name,read in json.loads(os.environ["FIXTURE_TOOLS"])]
@server.call_tool(validate_input=False)
async def call(name,arguments):
    return CallToolResult(content=[TextContent(type="text",text=json.dumps({"tool":name,"arguments":arguments}))])
async def main():
    async with stdio_server() as (read,write):
        await server.run(read,write,server.create_initialization_options())
asyncio.run(main())
''', encoding='utf-8')
    return script


@pytest.mark.parametrize('recipe,inputs,primary,extra,write', [
    ('db-postgres', {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'}, 'execute_sql', 'list_schemas', 'drop_database'),
    ('cache-redis', {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True}, 'get', 'scan_keys', 'set'),
    ('document-confluence', {'server': 'https://docs.example.test/wiki', 'username': 'reader@example.test'}, 'confluence_get_page', 'confluence_search', 'confluence_delete_page'),
])
def test_new_setup_discovers_read_tools_and_keeps_existing_capability_alias(registry, tmp_path, recipe, inputs, primary, extra, write):
    resource = build_setup_resource(registry.store, recipe, 'service', 'resource', inputs)
    assert resource['discover_tools'] is True
    script = lower_fixture(tmp_path)
    resource['connection'].update(command=sys.executable, args=['-I', str(script)])
    resource['connection']['env']['FIXTURE_TOOLS'] = json.dumps([[primary, True], [extra, True], [write, False]])
    profile = registry.store.put_profile('service', resources=[resource])
    registry.diagnose(profile, 'resource')
    capabilities = registry.list(profile)[0]['capability_summaries']
    original_alias = resource['capabilities'][0]['capability_id']
    assert {item['capability_id'] for item in capabilities} == {original_alias, extra, '*'}
    invocation = registry.normalize(profile, 'resource', extra, {}, {})
    assert invocation.prepared_request['tool_name'] == extra
    assert registry.execute(invocation)['result']['data']['tool'] == extra
    with pytest.raises(StoreError, match='NOT_FOUND'):
        registry.get(profile, 'resource', write)


def test_redis_missing_annotations_use_verified_read_metadata():
    adapter = get_adapter('redis_read')
    for name in ('scan_keys', 'scan_all_keys', 'hexists', 'llen', 'get_indexes', 'xrange'):
        assert adapter.is_read_tool({'name': name})
    for name in ('set', 'delete', 'publish', 'xreadgroup', 'search_redis_documents'):
        assert not adapter.is_read_tool({'name': name, 'annotations': {'readOnlyHint': True}})


def test_old_registration_requires_explicit_rediagnosis(registry, tmp_path):
    resource = build_setup_resource(registry.store, 'cache-redis', 'service', 'cache',
                                    {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True})
    resource['connection'].update(command=sys.executable, args=['-I', str(lower_fixture(tmp_path))])
    resource['connection']['env']['FIXTURE_TOOLS'] = json.dumps([['get', True]])
    profile = registry.store.put_profile('service', resources=[resource])
    registry.diagnose(profile, 'cache')
    path = registry._path(profile, 'cache')
    saved = json.loads(path.read_text())
    saved.pop('normalization_contract')
    path.write_text(json.dumps(saved))
    with pytest.raises(StoreError, match='CONTEXT_CHANGED'):
        registry.get(profile, 'cache', 'read-key')
    assert registry.diagnose(profile, 'cache')['ready'] is True


def test_read_bundle_pins_the_diagnosed_members_and_cannot_be_dispatched(registry, tmp_path):
    resource = build_setup_resource(registry.store, 'cache-redis', 'service', 'cache',
                                    {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True})
    resource['connection'].update(command=sys.executable, args=['-I', str(lower_fixture(tmp_path))])
    resource['connection']['env']['FIXTURE_TOOLS'] = json.dumps([['get', True], ['scan_keys', True]])
    profile = registry.store.put_profile('service', resources=[resource])
    registry.diagnose(profile, 'cache')
    bundle = registry.get(profile, 'cache', '*')
    assert bundle['capability_id'] == '*'
    assert bundle['capability_refs'] == [
        {'capability_id': name, 'contract_ref': registry.get(profile, 'cache', name)['contract_ref']}
        for name in ('read-key', 'scan_keys')]
    assert bundle['effects']['changes_state'] is False
    assert bundle['scope_schema']['additionalProperties'] is False
    assert any(item['capability_id'] == '*' for item in registry.list(profile)[0]['capability_summaries'])
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        registry.normalize(profile, 'cache', '*', {}, {})
    old_contract = bundle['contract_ref']
    resource['connection']['env']['FIXTURE_TOOLS'] = json.dumps([['get', True], ['scan_keys', True], ['info', True]])
    changed = registry.store.put_profile('service', resources=[resource])
    registry.diagnose(changed, 'cache')
    assert registry.get(changed, 'cache', '*')['contract_ref'] != old_contract


@pytest.mark.parametrize('recipe,inputs,field', [
    ('document-confluence', {'server': 'https://docs.example.test/wiki', 'username': 'reader@example.test'}, 'READ_ONLY_MODE'),
    ('issue-jira', {'server': 'https://jira.example.test', 'auth_type': 'api_token', 'username': 'reader@example.test'}, 'READ_ONLY_MODE'),
])
def test_lower_read_only_mode_cannot_be_disabled(registry, recipe, inputs, field):
    resource = build_setup_resource(registry.store, recipe, 'service', 'resource', inputs)
    resource['connection']['env'][field] = 'false'
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        validate_resource(resource)


def test_standard_nontext_mcp_content_is_preserved_as_json():
    adapter = get_adapter('confluence_read')
    result = CallToolResult(content=[
        TextContent(type='text', text='attachment'),
        EmbeddedResource(type='resource', resource=BlobResourceContents(uri='attachment:///123/report.pdf', mimeType='application/pdf', blob='YWJj')),
        ImageContent(type='image', mimeType='image/png', data='YWJj'),
    ])
    value, schema = adapter.decode_result(result, {}, {})
    assert value['data']['content'] == [item.model_dump(mode='json', by_alias=True, exclude_none=True) for item in result.content]
    assert schema['type'] == 'object'


def test_native_structured_schema_does_not_silently_discard_nontext_content():
    result = CallToolResult(content=[ImageContent(type='image', mimeType='image/png', data='YWJj')],
                            structuredContent={'id': '123'})
    tool = {'outputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}}
    with pytest.raises(StoreError, match='RESULT_VALIDATION_FAILED'):
        get_adapter('confluence_read').decode_result(result, tool, {})


@pytest.mark.parametrize('body', ['Error getting field x from hash cache: WRONGTYPE',
                                  'Error scanning keys with pattern cache:*: NOAUTH',
                                  '{"error":"NOAUTH Authentication required"}'])
def test_reused_redis_read_tools_report_their_provider_errors(body):
    adapter = get_adapter('redis_read')
    result = CallToolResult(content=[TextContent(type='text', text=body)])
    with pytest.raises(StoreError, match='EXTERNAL_EXECUTION_FAILED'):
        value, _ = adapter.decode_result(result, {}, {})
        adapter.provider_error(value)


def test_explicit_setup_refresh_migrates_old_generated_defaults(registry):
    values = {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'}
    old = build_setup_resource(registry.store, 'db-postgres', 'service', 'db', values)
    old.pop('discover_tools', None)
    old['capabilities'][0]['constraints'] = {'sql_field': 'sql', 'tables': ['*'], 'columns': ['*'], 'max_rows': 100}
    ordinary = build_setup_resource(registry.store, 'db-postgres', 'service', 'db', {}, existing=old)
    assert ordinary == old
    refreshed = build_setup_resource(registry.store, 'db-postgres', 'service', 'db', {}, existing=old, refresh_contract=True)
    assert refreshed['discover_tools'] is True
    assert refreshed['capabilities'][0]['constraints'] == {}
    assert old['capabilities'][0]['constraints']['max_rows'] == 100


def test_explicit_setup_refresh_never_erases_administrator_restrictions(registry):
    old = build_setup_resource(registry.store, 'cache-redis', 'service', 'cache',
                               {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True})
    old.pop('discover_tools', None)
    old['capabilities'][0]['constraints'] = {'fixed': {'key': 'only-approved'}}
    refreshed = build_setup_resource(registry.store, 'cache-redis', 'service', 'cache', {}, existing=old, refresh_contract=True)
    assert refreshed['capabilities'][0]['constraints'] == old['capabilities'][0]['constraints']
    assert not refreshed.get('discover_tools', False)


def test_setup_refresh_rejects_custom_sql_semantic_limits(registry):
    values = {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'}
    old = build_setup_resource(registry.store, 'db-postgres', 'service', 'db', values)
    old['capabilities'][0]['constraints'] = {'tables': ['public.orders'], 'columns': ['id'], 'max_rows': 20}
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        build_setup_resource(registry.store, 'db-postgres', 'service', 'db', {}, existing=old, refresh_contract=True)
