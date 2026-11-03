"""Real local HTTP transport checks for authentication and server-origin confirmation."""

from contextlib import asynccontextmanager
from datetime import timedelta
import importlib
import socket
import time

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult
import pytest
import uvicorn

from daeri.core.access import AuthenticatedUser
from daeri.infrastructure.auth import ProjectAccessToken


class Verifier:
    async def verify_token(self, token):
        if token not in ('alice', 'alice-new', 'alice-renewed', 'alice-less-scope', 'bob', 'no-scope'):
            return None
        actor = 'bob' if token == 'bob' else 'alice'
        projects = frozenset({'payments' if token == 'alice-new' else 'charging'})
        expires = int(time.time()) + (240 if token == 'alice-renewed' else 120)
        scopes = ['identity:read', 'daeri:use'] if token == 'alice-renewed' else ['daeri:use', 'identity:read']
        if token == 'alice-less-scope':
            scopes = ['daeri:use']
        return ProjectAccessToken(
            token=token, client_id='company-plugin',
            scopes=[] if token == 'no-scope' else scopes,
            subject=actor, expires_at=expires, resource='https://daeri.example/mcp',
            claims={'iss': 'https://company.example'},
            user=AuthenticatedUser(actor, projects, expires),
        )


class Agent:
    def __init__(self):
        self.resumed = False

    def projects(self, user):
        return [{'id': context.project_id, 'name': context.project_id.title()}
                for context in user.contexts()]

    def work(self, user, project_id, request, task_id=None):
        context = user.context(project_id)
        return {'task_id': task_id or 'task-1', 'actor_id': context.actor_id,
                'request': request, 'awaiting_confirmation': True, 'expires_at': user.expires_at}

    async def confirm(self, user, project_id, task_id, ask):
        user.context(project_id)
        reply = await ask('charging: DB 스키마를 조회하고 결과를 보고한다.')
        self.resumed = reply is not None
        return {'task_id': task_id, 'awaiting_confirmation': reply is None,
                'decision': reply.decision if reply else None,
                'response': reply.response if reply else ''}


def factory(agent=None):
    module = importlib.import_module('daeri.infrastructure.mcp_server')
    return module.build_mcp(agent or Agent(), Verifier(), 'https://company.example',
                            'https://daeri.example/mcp', ['daeri:use'])


@asynccontextmanager
async def serving(server):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    process = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level='critical'))
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(process.serve, [listener])
        try:
            with anyio.fail_after(5):
                while not process.started:
                    await anyio.sleep(0.01)
            yield 'http://127.0.0.1:' + str(port) + '/mcp'
        finally:
            process.should_exit = True
    listener.close()


@asynccontextmanager
async def connected(url, *, token='alice', callback=None):
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token,
                                          'Host': 'daeri.example'}, timeout=5) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, session_id):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=3),
                                     elicitation_callback=callback) as session:
                await session.initialize()
                yield session, http, session_id


def test_http_requires_verified_bearer_and_scope_and_advertises_metadata():
    async def check():
        async with serving(factory()) as url:
            async with httpx.AsyncClient() as http:
                for token, expected in [('', 401), ('invalid', 401), ('no-scope', 403)]:
                    headers = {'Authorization': 'Bearer ' + token} if token else {}
                    response = await http.post(url, json={}, headers=headers)
                    assert response.status_code == expected
                    if expected == 401:
                        assert 'resource_metadata=' in response.headers['www-authenticate']
                metadata = await http.get(url.removesuffix('/mcp') + '/.well-known/oauth-protected-resource/mcp')
                assert metadata.status_code == 200
                assert metadata.json()['authorization_servers'] == ['https://company.example/']
                assert metadata.json()['resource'] == 'https://daeri.example/mcp'
    anyio.run(check)


def test_configured_public_host_is_accepted_and_other_hosts_or_origins_are_rejected():
    async def check():
        async with serving(factory()) as url:
            async with httpx.AsyncClient() as http:
                base = {'Authorization': 'Bearer alice', 'Accept': 'application/json, text/event-stream'}
                initialize = {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                              'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                                         'clientInfo': {'name': 'test', 'version': 'test'}}}
                public = await http.post(url, headers={**base, 'Host': 'daeri.example'}, json=initialize)
                assert public.status_code == 200
                wrong_host = await http.post(url, headers={**base, 'Host': 'attacker.example'}, json=initialize)
                assert wrong_host.status_code == 421
                wrong_origin = await http.post(url, headers={**base, 'Host': 'daeri.example',
                                                             'Origin': 'https://attacker.example'}, json=initialize)
                assert wrong_origin.status_code == 403
    anyio.run(check)


def test_public_tools_exclude_caller_grants_and_decisions_and_use_current_request_identity():
    async def check():
        async with serving(factory()) as url:
            async with connected(url) as (session, http, session_id):
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert set(tools) == {
                    'daeri_projects', 'daeri_tasks', 'daeri_work', 'daeri_status',
                    'daeri_confirm', 'daeri_recover', 'daeri_files', 'daeri_download',
                }
                for tool in tools.values():
                    assert not {'actor_id', 'user', 'grants', 'tool_results', 'approved', 'decision'} & tool.inputSchema.get('properties', {}).keys()
                initial = await session.call_tool('daeri_projects')
                assert initial.structuredContent == {'projects': [{'id': 'charging', 'name': 'Charging'}]}
                forged = await session.call_tool('daeri_work', {
                    'project_id': 'charging', 'request': '운영 확인',
                    'actor_id': 'bob', 'approved': True,
                })
                assert forged.structuredContent['actor_id'] == 'alice'
                http.headers['Authorization'] = 'Bearer alice-renewed'
                refreshed = await session.call_tool('daeri_work', {
                    'project_id': 'charging', 'request': 'Check current identity',
                })
                assert refreshed.structuredContent['actor_id'] == 'alice'
                assert refreshed.structuredContent['expires_at'] >= forged.structuredContent['expires_at'] + 120
                http.headers['Authorization'] = 'Bearer bob'
                response = await http.post(url, headers={
                    'Mcp-Session-Id': session_id(),
                    'Accept': 'application/json, text/event-stream',
                }, json={'jsonrpc': '2.0', 'id': 99, 'method': 'tools/list'})
                assert response.status_code == 404
                http.headers['Authorization'] = 'Bearer alice-renewed'
    anyio.run(check)


@pytest.mark.parametrize('action,content,decision', [
    ('accept', {'decision': 'approve', 'response': '진행해 주세요'}, 'approve'),
    ('accept', {'decision': 'revise', 'response': '조회 기간을 줄여 주세요'}, 'revise'),
    ('accept', {'decision': 'reject'}, 'reject'),
    ('decline', None, None), ('cancel', None, None),
    ('accept', {}, None), ('accept', {'decision': 'approve', 'approved': True}, None),
    ('accept', {'decision': 'yes'}, None),
])
def test_only_valid_server_requested_decisions_resume_the_current_confirmation(action, content, decision):
    async def check():
        prompts = []

        async def callback(context, params):
            prompts.append(params)
            assert params.message == 'charging: DB 스키마를 조회하고 결과를 보고한다.'
            schema = params.requestedSchema
            assert schema['required'] == ['decision']
            assert schema['properties']['decision']['enum'] == ['approve', 'revise', 'reject']
            assert 'default' not in schema['properties']['decision']
            return ElicitResult(action=action, content=content)

        agent = Agent()
        async with serving(factory(agent)) as url:
            async with connected(url, callback=callback) as (session, _, __):
                reply = await session.call_tool('daeri_confirm', {'project_id': 'charging', 'task_id': 'task-1'})
                assert not reply.isError
                assert len(prompts) == 1
                assert reply.structuredContent['decision'] == decision
                assert agent.resumed == (decision is not None)
                assert reply.structuredContent['awaiting_confirmation'] == (decision is None)
    anyio.run(check)


def test_missing_elicitation_support_preserves_confirmation_wait():
    async def check():
        agent = Agent()
        async with serving(factory(agent)) as url:
            async with connected(url) as (session, _, __):
                reply = await session.call_tool('daeri_confirm', {'project_id': 'charging', 'task_id': 'task-1'})
                assert not reply.isError
                assert reply.structuredContent['awaiting_confirmation'] is True
                assert agent.resumed is False
    anyio.run(check)


@pytest.mark.parametrize('refresh', [False, True])
def test_http_confirmation_persists_the_actual_response_before_the_guarded_harness_resume(refresh):
    from daeri.core.cloud import HarnessReply
    from test_agent_service import setup, PLAN, PENDING

    async def check():
        service, _, store, harness = setup(HarnessReply('계획을 확인해 주세요.', 'tool_use', (PENDING,)),
                                            HarnessReply('검증 결과입니다.', 'end_turn'))
        http = None

        async def callback(context, params):
            assert params.message == PLAN
            if refresh:
                http.headers['Authorization'] = 'Bearer alice-renewed'
            return ElicitResult(action='accept', content={'decision': 'approve'})

        async with serving(factory(service)) as url:
            async with connected(url, callback=callback) as (session, http, __):
                started = await session.call_tool('daeri_work', {
                    'project_id': 'charging', 'request': '캐시를 비교해 주세요.',
                })
                assert not started.isError
                task_id = started.structuredContent['task_id']
                assert started.structuredContent['awaiting_confirmation']
                assert harness.guards[0]['decision'] == 'deny'
                confirmed = await session.call_tool('daeri_confirm', {
                    'project_id': 'charging', 'task_id': task_id,
                })
                assert not confirmed.isError
                assert confirmed.structuredContent['response'] == '검증 결과입니다.'
                assert confirmed.structuredContent['awaiting_confirmation'] is False
                assert harness.guards[1]['decision'] == 'allow'
                status = await session.call_tool('daeri_status', {'project_id': 'charging', 'task_id': task_id})
                assert status.structuredContent['state'] == 'active'
                assert status.structuredContent['awaiting_confirmation'] is False
                assert status.structuredContent['task_id'] == task_id
                listed = await session.call_tool('daeri_tasks', {'project_id': 'charging'})
                assert listed.structuredContent['items'][0]['task_id'] == task_id
    anyio.run(check)


@pytest.mark.parametrize('response_token', ['alice-new', 'alice-less-scope'])
def test_reduced_authority_on_the_form_response_cannot_resume_original_project(response_token):
    from daeri.core.cloud import HarnessReply
    from test_agent_service import setup, PENDING

    async def check():
        service, _, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
                                            HarnessReply('Should not execute', 'end_turn'))
        prompted = False
        http = None

        async def callback(context, params):
            nonlocal prompted
            prompted = True
            http.headers['Authorization'] = 'Bearer ' + response_token
            return ElicitResult(action='accept', content={'decision': 'approve'})

        try:
            async with serving(factory(service)) as url:
                async with connected(url, callback=callback) as (session, http, _):
                    started = await session.call_tool('daeri_work', {
                        'project_id': 'charging', 'request': 'Compare cache',
                    })
                    with anyio.move_on_after(1):
                        await session.call_tool('daeri_confirm', {
                            'project_id': 'charging', 'task_id': started.structuredContent['task_id'],
                        })
        except ExceptionGroup:
            # The client can abort the transport when its reply POST is refused.
            pass
        assert prompted
        assert len(harness.calls) == 1
        original = harness.calls[0][0]
        assert store.get_task(original.context, original.id).approval.decision == 'pending'
    anyio.run(check)


def test_runtime_public_resource_uses_internal_mcp_transport_and_real_confirmation():
    from daeri.core.endpoints import runtime_endpoint
    from daeri.infrastructure.mcp_server import build_mcp

    resource = runtime_endpoint('arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/daeri-1234567890', 'PROD')
    class RuntimeVerifier(Verifier):
        async def verify_token(self, raw):
            token = await super().verify_token(raw)
            return token.model_copy(update={'resource': resource}) if token else None
    agent = Agent()
    server = build_mcp(agent, RuntimeVerifier(), 'https://company.example', resource, ['daeri:use'])
    async def check():
        async def answer(context, params):
            assert 'DB 스키마' in params.message
            return ElicitResult(action='accept', content={'decision': 'approve', 'response': ''})
        async with serving(server) as url:
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer alice'}, timeout=5) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write, elicitation_callback=answer) as session:
                        await session.initialize()
                        projects = await session.call_tool('daeri_projects')
                        assert projects.structuredContent['projects'][0]['id'] == 'charging'
                        result = await session.call_tool('daeri_confirm', {'project_id': 'charging', 'task_id': 'task-1'})
                        assert result.structuredContent['decision'] == 'approve'
        assert agent.resumed
    anyio.run(check)
