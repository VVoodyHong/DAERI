"""Project connection changes require a current manager and a real host response."""

import asyncio
import time

import pytest

from daeri.core.access import AuthenticatedUser
from daeri.core.errors import StoreError


class Catalog:
    def get(self, project_id):
        if project_id != 'example':
            raise StoreError('PROJECT_UNAVAILABLE', 'Project is unavailable.')
        return object()


class Backend:
    def __init__(self):
        self.saved = []

    def describe(self, project_id, connector_id=None):
        return {'name': 'Database', 'fields': {'host': {'title': '호스트', 'type': 'string', 'required': True}},
                'credentials': []}

    def list(self, project_id):
        return [{'id': 'db', 'state': 'READY', 'resource_access': 'not_checked'}]

    def connect(self, project_id, connection_id, connector_id, inputs, *, expected_revision=None):
        self.saved.append((project_id, connection_id, connector_id, inputs))
        return {'id': connection_id, 'state': 'READY', 'resource_access': 'not_checked'}


def manager():
    return AuthenticatedUser('owner', frozenset({'example'}), int(time.time()) + 60,
                             managed_project_ids=frozenset({'example'}))


def service():
    from daeri.application.connections import ConnectionService
    backend = Backend()
    return ConnectionService(Catalog(), backend), backend


def test_members_reuse_project_connections_but_only_manager_can_change_them():
    app, backend = service()
    member = AuthenticatedUser('member', frozenset({'example'}), int(time.time()) + 60)
    assert app.connections(member, 'example')['connections'][0]['id'] == 'db'
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        app.connectors(member, 'example')

    async def unexpected(form):
        pytest.fail('An ordinary member was asked to change a shared connection.')
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        asyncio.run(app.configure(member, 'example', 'db', 'postgres', unexpected))
    assert backend.saved == []


def test_manager_response_applies_inputs_to_project_and_cancel_keeps_configuration():
    app, backend = service()

    async def accept(form):
        assert form['fields']['host']['title'] == '호스트'
        return {'host': 'db.example.test'}
    result = asyncio.run(app.configure(manager(), 'example', 'db', 'postgres', accept))
    assert result['id'] == 'db'
    assert backend.saved == [('example', 'db', 'postgres', {'host': 'db.example.test'})]

    async def cancel(form):
        return None
    assert asyncio.run(app.configure(manager(), 'example', 'db', 'postgres', cancel))['state'] == 'cancelled'
    assert len(backend.saved) == 1


def test_expired_confirmation_and_unavailable_project_cannot_apply_connections(monkeypatch):
    app, backend = service()
    user = manager()

    async def expire(form):
        monkeypatch.setattr('daeri.core.access.time.time', lambda: user.expires_at + 1)
        return {'host': 'db.example.test'}
    with pytest.raises(StoreError, match='AUTHENTICATION_REQUIRED'):
        asyncio.run(app.configure(user, 'example', 'db', 'postgres', expire))
    assert backend.saved == []


def test_form_failure_does_not_apply_connection():
    app, backend = service()

    async def disconnect(form):
        raise ConnectionError('Disconnected')
    with pytest.raises(ConnectionError):
        asyncio.run(app.configure(manager(), 'example', 'db', 'postgres', disconnect))
    assert backend.saved == []


def test_invalid_connection_identifier_is_rejected_before_the_form():
    app, backend = service()

    async def unexpected(form):
        pytest.fail('An invalid identifier opened a connection form.')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        asyncio.run(app.configure(manager(), 'example', '../db', 'postgres', unexpected))
    assert backend.saved == []


def test_operator_configuration_revision_change_while_form_is_open_is_rejected():
    app, backend = service()
    original = backend.describe
    revision = ['before']
    backend.describe = lambda *args: {**original(*args), 'revision': revision[0]}

    async def changed(form):
        revision[0] = 'after'
        return {'host': 'db.example.test'}
    with pytest.raises(StoreError, match='CONNECTION_CHANGED'):
        asyncio.run(app.configure(manager(), 'example', 'db', 'postgres', changed))
    assert backend.saved == []


def test_form_revision_is_bound_to_the_backend_operation():
    app, backend = service()
    original = backend.describe
    backend.describe = lambda *args: {**original(*args), 'revision': 'confirmed-config'}
    expected = []
    connect = backend.connect

    def bound(*args, expected_revision=None):
        expected.append(expected_revision)
        return connect(*args, expected_revision=expected_revision)
    backend.connect = bound

    async def accept(form):
        return {'host': 'db.example.test'}
    asyncio.run(app.configure(manager(), 'example', 'db', 'postgres', accept))
    assert expected == ['confirmed-config']
