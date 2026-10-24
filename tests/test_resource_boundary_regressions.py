"""Unverified remote bindings and Confluence product errors fail closed."""

import copy
import sys

import pytest

from daeri.resources import ResourceRegistry, validate_resource
from daeri.storage import Store, StoreError


@pytest.fixture
def registry(tmp_path):
    store = Store(tmp_path / 'private')
    yield ResourceRegistry(store, credential_resolver=lambda reference: 'private-credential')
    store.close()


def remote_resource(adapter='gitlab_read', resource_type='Source', tool='get_repository_file'):
    return {
        'instance_id': 'remote', 'type': resource_type, 'adapter': adapter,
        'target': {'server': 'https://resource.example.test'},
        'identity_ref': 'identity:reader',
        'connection': {'transport': 'streamable_http', 'url': 'https://mcp.example.test/mcp',
                       'auth_headers': {'Authorization': 'credential:remote'}},
        'capabilities': [{'capability_id': 'read', 'tool_name': tool, 'constraints': {}}],
    }


@pytest.mark.parametrize('adapter,resource_type,tool', [
    ('postgres_read', 'DB', 'execute_sql'), ('redis_read', 'Cache', 'get'),
    ('aws_read', 'Infra', 'call_aws'), ('gitlab_read', 'Source', 'get_repository_file'),
    ('confluence_read', 'Document', 'confluence_get_page'), ('jira_read', 'Issue', 'jira_get_issue'),
])
def test_builtin_adapters_reject_remote_bindings_they_cannot_verify(adapter, resource_type, tool):
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        validate_resource(remote_resource(adapter, resource_type, tool))


@pytest.mark.parametrize('endpoint', ['https://mcp.example.test/mcp', 'https://resource.example.test/mcp'])
def test_remote_tool_metadata_and_endpoint_host_do_not_prove_resource_identity(registry, monkeypatch, endpoint):
    resource = remote_resource()
    resource['connection']['url'] = endpoint
    profile = {'profile_id': 'profile_remote', 'revision': 1, 'resources': [resource]}

    async def metadata_only_session(*args):
        return object(), []

    async def tools(session):
        return [{'name': 'get_repository_file', 'inputSchema': {'type': 'object'}}]

    monkeypatch.setattr(registry, '_session', metadata_only_session)
    monkeypatch.setattr(registry, '_tools', tools)
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        registry.diagnose(profile, 'remote')
    assert not list(registry.directory.glob('*.json'))


@pytest.fixture(params=['legacy_text', 'wrapped_string', 'structured'])
def confluence_connection(registry, tmp_path, request):
    script = tmp_path / 'confluence_mcp.py'
    script.write_text('''import asyncio,json,os
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
server = Server("confluence-contract-fixture")
mode = os.environ['FIXTURE_MODE']
@server.list_tools()
async def tools():
    output = None
    if mode == 'wrapped_string':
        output = {"type":"object","properties":{"result":{"type":"string"}},
                  "required":["result"],"additionalProperties":False,"x-fastmcp-wrap-result":True}
    elif mode == 'structured':
        output = {"type":"object","properties":{"metadata":{"type":"object"},"error":{"type":"string"}},
                  "additionalProperties":False}
    return [Tool(name="confluence_get_page", inputSchema={"type":"object", "properties":{"page_id":{"type":"string"}},
                 "required":["page_id"],"additionalProperties":False}, outputSchema=output)]
@server.call_tool(validate_input=False)
async def call(name, arguments):
    if arguments['page_id'] == '404':
        value = {"error":"Page not found; password=private-error-body"}
    else:
        value = {"metadata":{"id":"123","title":"Runbook","content":"error: document text is data"}}
    text = json.dumps(value)
    structured = {"result":text} if mode == 'wrapped_string' else value if mode == 'structured' else None
    return CallToolResult(content=[TextContent(type="text",text=text)], structuredContent=structured)
async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
asyncio.run(main())
''')
    resource = {
        'instance_id': 'document', 'type': 'Document', 'adapter': 'confluence_read',
        'target': {'server': 'https://docs.example.test/wiki'}, 'identity_ref': 'identity:reader',
        'connection': {'transport': 'stdio', 'command': sys.executable, 'args': ['-I', str(script)],
                       'env': {'CONFLUENCE_URL': 'https://docs.example.test/wiki', 'FIXTURE_MODE': request.param}},
        'capabilities': [{'capability_id': 'read-page', 'tool_name': 'confluence_get_page', 'constraints': {}}],
    }
    profile = {'profile_id': 'profile_document', 'revision': 1, 'resources': [resource]}
    registry.diagnose(profile, 'document')
    return profile, request.param


def test_confluence_product_error_is_failed_without_exposing_its_body(registry, confluence_connection):
    profile, _ = confluence_connection
    invocation = registry.normalize(profile, 'document', 'read-page', {'page_id': '404'},
                                    {'equals': {'page_id': '404'}})
    with pytest.raises(StoreError, match='EXTERNAL_EXECUTION_FAILED') as caught:
        registry.execute(invocation)
    assert 'private-error-body' not in str(caught.value)
    assert 'Page not found' not in str(caught.value)


def test_confluence_success_keeps_document_content_and_unknown_completeness(registry, confluence_connection):
    profile, mode = confluence_connection
    invocation = registry.normalize(profile, 'document', 'read-page', {'page_id': '123'},
                                    {'equals': {'page_id': '123'}})
    result = registry.execute(invocation)
    expected = {'metadata': {'id': '123', 'title': 'Runbook', 'content': 'error: document text is data'}}
    assert result['result'] == (expected if mode == 'structured' else {'data': expected})
    assert result['source_complete'] is None


def test_confluence_product_error_persists_failed_call_without_usable_evidence(registry, confluence_connection):
    from daeri.engine import Engine
    from test_execution_flow import approve, begin, execution, resource as cache_resource

    source, _ = confluence_connection
    resource = copy.deepcopy(source['resources'][0])
    resource['capabilities'][0]['capability_id'] = 'get'
    resource['policy'] = cache_resource()['policy']
    profile = registry.store.put_profile('document-profile', resources=[resource])
    registry.diagnose(profile, 'document')
    engine = Engine(registry.store.data_dir)
    try:
        def page_scope(draft):
            draft['steps'][0]['scope'] = {'equals': {'page_id': '404'}}

        saved, draft = begin(engine, profile, configure=page_scope)
        approved = approve(engine, saved)
        args = execution(saved, approved, draft['steps'][0])
        args['parameters'] = {'page_id': '404'}
        result = engine.call('daeri_capability_execute', args)
        assert result['status'] == 'error'
        assert result['error']['code'] == 'EXTERNAL_EXECUTION_FAILED'
        assert result['data']['call_state'] == 'failed'
        assert result['data']['inline_result'] is None
        assert result['data']['evidence_refs'] == []
        assert 'private-error-body' not in repr(result)
        stored = engine.store.read_record(saved['task_id'], result['data']['result_ref'])['payload']
        assert stored['result'] is None
        assert stored['error_code'] == 'EXTERNAL_EXECUTION_FAILED'
        assert engine.call('daeri_capability_execute', args)['data']['call_state'] == 'failed'
    finally:
        engine.close()


def test_previously_normalized_remote_invocation_is_blocked_before_call(registry, confluence_connection, monkeypatch):
    from mcp.types import CallToolResult

    profile, mode = confluence_connection
    invocation = registry.normalize(profile, 'document', 'read-page', {'page_id': '123'},
                                    {'equals': {'page_id': '123'}})
    # Model an invocation retained from a version that accepted HTTPS on tools/list alone.
    invocation.resource['connection'] = remote_resource()['connection'] | {'auth_headers': {}}
    invocation.connection = copy.deepcopy(invocation.resource['connection'])

    class Session:
        async def send_request(self, *args):
            value = ({'result': '{"metadata":{"id":"123"}}'} if mode == 'wrapped_string'
                     else {'metadata': {'id': '123'}})
            return CallToolResult(content=[], structuredContent=value)

    async def remote_session(*args):
        return Session(), []

    async def tools(session):
        return [invocation.tool_contract]

    monkeypatch.setattr(registry, '_session', remote_session)
    monkeypatch.setattr(registry, '_tools', tools)
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        registry.execute(invocation)
