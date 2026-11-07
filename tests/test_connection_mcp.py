"""Connection forms keep trusted deployment settings out of model arguments."""

import time
from types import SimpleNamespace

import anyio
from mcp.types import ElicitResult
import pytest

from daeri.application.connections import ConnectionService
from daeri.core.access import AuthenticatedUser
from daeri.infrastructure.mcp_server import build_mcp
from test_mcp_server import Agent, Verifier, serving, connected


class ManagerVerifier(Verifier):
    async def verify_token(self, raw):
        token = await super().verify_token(raw)
        if token is not None and raw == 'alice':
            user = token.user
            token = token.model_copy(update={'user': AuthenticatedUser(
                user.actor_id, user.project_ids, user.expires_at, user.project_ids)})
        return token


class Backend:
    def __init__(self):
        self.saved = []

    def describe(self, project_id, connector_id=None):
        if connector_id is None:
            return {'connectors': [{'id': 'postgres', 'name': 'PostgreSQL'}], 'credentials': []}
        return {'name': 'PostgreSQL', 'fields': {
            'host': {'title': '호스트', 'type': 'string', 'required': True},
            'port': {'title': '포트', 'type': 'integer', 'required': False, 'default': 5432},
            'tls': {'title': 'TLS', 'type': 'boolean', 'required': False, 'default': True},
            'password': {'title': '비밀번호 참조', 'type': 'string', 'required': True, 'secret_ref': True},
        }, 'credentials': ['service-account']}

    def list(self, project_id):
        return [{'id': 'db', 'state': 'READY', 'resource_access': 'not_checked'}]

    def connect(self, project_id, connection_id, connector_id, inputs, *, expected_revision=None):
        self.saved.append((project_id, connection_id, connector_id, inputs))
        return {'id': connection_id, 'state': 'READY', 'resource_access': 'not_checked'}


def factory():
    class Catalog:
        def get(self, project_id):
            return object()
    backend = Backend()
    app = ConnectionService(Catalog(), backend)
    server = build_mcp(Agent(), ManagerVerifier(), 'https://company.example',
                       'https://daeri.example/mcp', ['daeri:use'], connections=app)
    return server, backend


def test_manager_registers_connection_from_real_form_and_members_reuse_list():
    async def check():
        server, backend = factory()

        async def answer(context, params):
            properties = params.requestedSchema['properties']
            assert properties['port']['type'] == 'integer'
            assert properties['tls']['type'] == 'boolean'
            assert properties['credential_id']['enum'] == ['service-account']
            assert '비밀값' in properties['password']['description']
            assert 'charging' in params.message and 'db' in params.message
            return ElicitResult(action='accept', content={
                'host': 'db.example.test', 'password': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:project/db-123456',
                'credential_id': 'service-account',
            })

        async with serving(server) as url:
            async with connected(url, callback=answer) as (session, _, __):
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert set(tools['daeri_connect'].inputSchema['properties']) == {
                    'project_id', 'connection_id', 'connector_id'}
                available = await session.call_tool('daeri_connectors', {'project_id': 'charging'})
                assert available.structuredContent['connectors'][0]['id'] == 'postgres'
                result = await session.call_tool('daeri_connect', {
                    'project_id': 'charging', 'connection_id': 'db', 'connector_id': 'postgres'})
                assert not result.isError
                assert result.structuredContent['state'] == 'READY'
                assert 'db.example.test' not in result.model_dump_json()
                assert backend.saved[0][3]['port'] == 5432
                assert backend.saved[0][3]['tls'] is True
            async with connected(url, token='bob') as (session, _, __):
                listed = await session.call_tool('daeri_connections', {'project_id': 'charging'})
                assert listed.structuredContent['connections'][0]['id'] == 'db'
                denied = await session.call_tool('daeri_connect', {
                    'project_id': 'charging', 'connection_id': 'db', 'connector_id': 'postgres'})
                assert denied.isError and denied.structuredContent['error']['code'] == 'ACCESS_DENIED'
        assert len(backend.saved) == 1
    anyio.run(check)


def test_expired_user_cannot_open_connection_form_and_gets_structured_auth_error(monkeypatch):
    async def check():
        server, backend = factory()

        async def unexpected_form(context, params):
            pytest.fail('An expired user received a connection form.')

        async with serving(server) as url:
            async with connected(url, callback=unexpected_form) as (session, _, __):
                monkeypatch.setattr('daeri.core.access.time', SimpleNamespace(time=lambda: time.time() + 600))
                result = await session.call_tool('daeri_connect', {
                    'project_id': 'charging', 'connection_id': 'db', 'connector_id': 'postgres'})
                assert result.isError and result.structuredContent
                assert result.structuredContent['error']['code'] == 'AUTHENTICATION_REQUIRED'
        assert backend.saved == []
    anyio.run(check)


@pytest.mark.parametrize('expires_during_form', [False, True])
def test_cancelled_connection_form_has_no_deployment_side_effect(monkeypatch, expires_during_form):
    async def check():
        server, backend = factory()

        async def cancel(context, params):
            if expires_during_form:
                monkeypatch.setattr('daeri.core.access.time', SimpleNamespace(time=lambda: time.time() + 600))
            return ElicitResult(action='cancel')
        async with serving(server) as url:
            async with connected(url, callback=cancel) as (session, _, __):
                result = await session.call_tool('daeri_connect', {
                    'project_id': 'charging', 'connection_id': 'db', 'connector_id': 'postgres'})
                if expires_during_form:
                    assert result.isError and result.structuredContent['error']['code'] == 'AUTHENTICATION_REQUIRED'
                else:
                    assert not result.isError and result.structuredContent['state'] == 'cancelled'
        assert backend.saved == []
    anyio.run(check)


@pytest.mark.parametrize('failure', ['unsupported', 'timeout'])
def test_connection_form_unavailability_cancels_without_saving(monkeypatch, failure):
    from mcp.server.fastmcp import Context

    async def timed_out(*args, **kwargs):
        raise TimeoutError

    if failure == 'timeout':
        monkeypatch.setattr(Context, 'elicit', timed_out)

    async def check():
        server, backend = factory()
        async with serving(server) as url:
            async with connected(url) as (session, _, __):
                result = await session.call_tool('daeri_connect', {
                    'project_id': 'charging', 'connection_id': 'db', 'connector_id': 'postgres'})
                assert not result.isError and result.structuredContent['state'] == 'cancelled'
        assert backend.saved == []
    anyio.run(check)


def test_management_revoked_during_form_cannot_apply_under_existing_session():
    async def check():
        server, backend = factory()
        http = None
        prompted = False

        async def revoke(context, params):
            nonlocal prompted
            prompted = True
            http.headers['Authorization'] = 'Bearer alice-renewed'
            return ElicitResult(action='accept', content={
                'host': 'db.example.test', 'password': 'secret-reference', 'credential_id': 'service-account'})
        try:
            async with serving(server) as url:
                async with connected(url, callback=revoke) as (session, http, _):
                    with anyio.move_on_after(1):
                        await session.call_tool('daeri_connect', {
                            'project_id': 'charging', 'connection_id': 'db', 'connector_id': 'postgres'})
        except ExceptionGroup:
            pass
        assert prompted and backend.saved == []
    anyio.run(check)


def test_operator_fields_with_model_method_names_round_trip_through_host_form():
    from daeri.infrastructure.mcp_server import _connection_form
    form = _connection_form({'fields': {
        'model_config': {'type': 'string', 'title': '모델 설정', 'required': True},
        'model_dump': {'type': 'string', 'title': '출력 설정', 'required': True},
    }})
    assert set(form.model_json_schema()['properties']) == {'model_config', 'model_dump'}
    value = form.model_validate({'model_config': 'example', 'model_dump': 'summary'})
    assert value.model_dump(by_alias=True) == {'model_config': 'example', 'model_dump': 'summary'}
