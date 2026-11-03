"""Inert remote settings and fail-closed managed harness readiness."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from yaml import safe_dump

from daeri.core.errors import StoreError


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/charging-1234567890'
HOOK = 'arn:aws:lambda:us-east-1:123456789012:function:daeri-approval'
ROUTE = SimpleNamespace(harness_arn=ARN, harness_endpoint='RELEASE_V2', harness_version='2')
BASE = {'public_url': 'https://daeri.example.com/mcp', 'storage': {'bucket': 'daeri-workspace'},
        'auth': {'issuer': 'https://login.example.com/company', 'audience': 'https://daeri.example.com/mcp'},
        'approval_hook_arn': HOOK}


def module():
    from daeri.config import server
    return server


def configured_harness():
    return {'arn': ARN, 'status': 'READY', 'harnessVersion': '2',
            'hooks': [{'beforeToolCall': {'name': 'approval',
                       'target': {'lambda': {'arn': HOOK, 'failureMode': 'deny'}}}}],
            'tools': [{'type': 'inline_function', 'name': 'confirm_plan', 'config': {
                'inlineFunction': {'description': 'Ask the real user to approve the current plan.',
                                   'inputSchema': {'type': 'object', 'properties': {'plan': {'type': 'string'}},
                                                   'required': ['plan']}}}}]}


class Control:
    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = []

    def get_harness(self, **request):
        self.calls.append(request)
        return {'harness': self.metadata}

    def get_harness_endpoint(self, **request):
        self.calls.append(request)
        return {'endpoint': {'harnessId': ARN.rsplit('/', 1)[1], 'endpointName': 'RELEASE_V2',
                             'status': 'READY', 'liveVersion': '2', 'targetVersion': '2'}}


def test_reading_server_settings_does_not_discover_issuer_or_create_aws_clients(tmp_path, monkeypatch):
    import httpx
    import boto3

    def unexpected(*args, **kwargs):
        raise AssertionError('Settings parsing performed network or AWS initialization')
    monkeypatch.setattr(httpx, 'get', unexpected)
    monkeypatch.setattr(boto3, 'Session', unexpected)
    path = tmp_path / 'server.yaml'
    path.write_text(safe_dump(BASE))
    settings = module().ServerSettings.from_yaml(path)
    assert settings.auth.project_claim == 'projects'
    assert settings.auth.required_scopes == ['daeri/use']
    assert settings.public_url == 'https://daeri.example.com/mcp'


@pytest.mark.parametrize('patch', [
    {'public_url': 'http://daeri.example.com/mcp'}, {'public_url': 'https://daeri.example.com/'},
    {'public_url': 'https://user:secret@daeri.example.com/mcp'},
    {'public_url': 'https://daeri.example.com/mcp?secret=value'},
    {'approval_hook_arn': 'arn:aws:sns:us-east-1:123456789012:topic'},
    {'auth': {'issuer': 'http://login.example.com', 'audience': 'daeri'}},
    {'auth': {'issuer': 'https://login.example.com?password=secret', 'audience': 'daeri'}},
    {'auth': {'issuer': 'https://login.example.com', 'audience': ''}},
    {'auth': {'issuer': 'https://login.example.com', 'audience': 'daeri', 'required_scopes': []}},
    {'auth': {'issuer': 'https://login.example.com', 'audience': 'daeri', 'required_scopes': ['bad scope']}},
    {'auth': {'issuer': 'https://login.example.com', 'audience': 'daeri', 'required_scopes': ['use', 'use']}},
])
def test_invalid_remote_settings_fail_before_service_composition(patch):
    with pytest.raises(ValidationError):
        module().ServerSettings.model_validate({**BASE, **patch})


def test_ready_harness_requires_the_configured_deny_hook_and_inline_plan_schema():
    client = Control(configured_harness())
    readiness = module().HarnessReadiness(client, HOOK)
    readiness.check(ROUTE)
    assert client.calls == [{'harnessId': 'charging-1234567890', 'endpointName': 'RELEASE_V2'},
                            {'harnessId': 'charging-1234567890', 'harnessVersion': '2'}]


@pytest.mark.parametrize('change', [
    'missing_hook', 'wrong_hook', 'fail_open', 'not_ready', 'wrong_harness',
    'missing_plan', 'non_inline', 'optional_plan', 'non_string_plan',
])
def test_unsafe_managed_harness_configuration_cannot_start_a_public_workflow(change):
    metadata = deepcopy(configured_harness())
    if change == 'missing_hook': metadata['hooks'] = []
    elif change == 'wrong_hook': metadata['hooks'][0]['beforeToolCall']['target']['lambda']['arn'] += '-other'
    elif change == 'fail_open': metadata['hooks'][0]['beforeToolCall']['target']['lambda']['failureMode'] = 'allow'
    elif change == 'not_ready': metadata['status'] = 'UPDATING'
    elif change == 'wrong_harness': metadata['arn'] += '-other'
    elif change == 'missing_plan': metadata['tools'] = []
    elif change == 'non_inline': metadata['tools'][0]['type'] = 'remote_mcp'
    elif change == 'optional_plan': metadata['tools'][0]['config']['inlineFunction']['inputSchema']['required'] = []
    elif change == 'non_string_plan': metadata['tools'][0]['config']['inlineFunction']['inputSchema']['properties']['plan']['type'] = 'boolean'
    with pytest.raises(StoreError, match='HARNESS_NOT_READY'):
        module().HarnessReadiness(Control(metadata), HOOK).check(ROUTE)


def test_readiness_lookup_failure_never_exposes_private_provider_details():
    class Broken:
        def get_harness(self, **request):
            raise RuntimeError('password=PRIVATE')
    with pytest.raises(StoreError) as error:
        module().HarnessReadiness(Broken(), HOOK).check(ROUTE)
    assert error.value.code == 'HARNESS_NOT_READY' and 'PRIVATE' not in str(error.value)


class ServiceClient:
    def __init__(self, service):
        self.service, self.closed = service, False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, fail_service=None):
        self.clients, self.fail_service = {}, fail_service

    def client(self, service, **options):
        if service == self.fail_service:
            raise RuntimeError('Controlled client initialization failure')
        value = self.clients[service] = ServiceClient(service)
        return value


def test_server_composition_is_inert_and_closes_all_managed_clients(monkeypatch):
    import httpx

    def unexpected(*args, **kwargs):
        raise AssertionError('Service composition attempted OIDC network access')
    monkeypatch.setattr(httpx, 'get', unexpected)
    session = Session()
    settings = module().ServerSettings.model_validate(BASE)
    with module().build_server(settings, session=session) as services:
        assert services.cloud.workflow.store is services.cloud.store
        assert services.token_verifier.resource_url == 'https://daeri.example.com/mcp'
        assert services.mcp.name == 'DAERI'
        assert set(session.clients) == {'s3', 'ssm', 'bedrock-agentcore', 'bedrock-agentcore-control'}
        assert not any(client.closed for client in session.clients.values())
    assert all(client.closed for client in session.clients.values())
    services.close()


def test_partial_server_composition_failure_closes_prior_clients():
    session = Session(fail_service='bedrock-agentcore-control')
    with pytest.raises(RuntimeError, match='Controlled client initialization failure'):
        module().build_server(module().ServerSettings.model_validate(BASE), session=session)
    assert set(session.clients) == {'s3', 'ssm', 'bedrock-agentcore'}
    assert all(client.closed for client in session.clients.values())


def test_changed_release_endpoint_cannot_resume_saved_task_version():
    client = Control(configured_harness())
    client.get_harness_endpoint = lambda **request: {'endpoint': {
        'harnessId': ARN.rsplit('/', 1)[1], 'endpointName': 'RELEASE_V2',
        'status': 'READY', 'liveVersion': '3', 'targetVersion': '3'}}
    with pytest.raises(StoreError, match='HARNESS_NOT_READY'):
        module().HarnessReadiness(client, HOOK).check(ROUTE)
    assert client.calls == []


def test_legacy_task_without_saved_release_remains_readable_but_cannot_execute():
    route = SimpleNamespace(harness_arn=ARN, harness_endpoint='', harness_version='')
    with pytest.raises(StoreError, match='HARNESS_NOT_READY'):
        module().HarnessReadiness(Control(configured_harness()), HOOK).check(route)


def test_public_workflow_uses_saved_release_and_rejects_endpoint_drift_before_approval():
    import asyncio
    from daeri.core.cloud import HarnessReply
    from daeri.core.projects import Project
    from test_agent_service import setup, answer, PENDING

    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
                                          HarnessReply('Complete', 'end_turn'))
    service.workflow.catalog.get = lambda project_id: Project(project_id, 'Project', ARN, 'RELEASE_V2', '2')
    client = Control(configured_harness())
    service.check_harness = module().HarnessReadiness(client, HOOK).check
    first = service.work(user, 'project', 'Compare project data')
    task = store.get_task(user.context('project'), first['task_id'])
    assert task.harness_endpoint == 'RELEASE_V2' and task.harness_version == '2'
    original = client.get_harness_endpoint
    client.get_harness_endpoint = lambda **request: {'endpoint': {
        'harnessId': ARN.rsplit('/', 1)[1], 'endpointName': 'RELEASE_V2',
        'status': 'READY', 'liveVersion': '3', 'targetVersion': '3'}}
    with pytest.raises(StoreError, match='HARNESS_NOT_READY'):
        asyncio.run(service.confirm(user, 'project', task.id, answer))
    assert len(harness.calls) == 1
    assert store.get_task(user.context('project'), task.id).approval.decision == 'pending'
    client.get_harness_endpoint = original
    result = asyncio.run(service.confirm(user, 'project', task.id, answer))
    assert result['response'] == 'Complete'
    assert harness.guards[1]['decision'] == 'allow'
