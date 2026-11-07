"""Managed subagents are fixed project targets behind the approved Gateway call."""

from contextlib import contextmanager
from copy import deepcopy
import json
import re
import sys
from types import SimpleNamespace

import botocore.session
from botocore.exceptions import PaginationError
from botocore.validate import validate_parameters
import pytest

from aws_paginator import PaginatedClient
from daeri.core.cloud import HarnessReply
from daeri.core.errors import StoreError
from daeri.core.projects import Project


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/Research-abcdefghij'
ROOT = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/Root-abcdefghij'
MEMORY = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/Project-abcdefghij'
GATEWAY_ID = 'project-abcdefghij'
GATEWAY = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/' + GATEWAY_ID
TARGET_ID = '0123456789'
LAMBDA = 'arn:aws:lambda:us-east-1:123456789012:function:delegation'
CONFIG = {'project_id': 'payments', 'gateway_arn': GATEWAY, 'target_name': 'subagents',
          'lambda_arn': LAMBDA, 'subagents': {'research': {'description': 'Research project facts',
          'harness_arn': ARN, 'harness_endpoint': 'RELEASE_V2', 'harness_version': '2'}}}


class Control:
    def get_harness_endpoint(self, **request):
        return {'endpoint': {'status': 'READY', 'liveVersion': '2', 'targetVersion': '2'}}


def context(**patch):
    return SimpleNamespace(client_context=SimpleNamespace(custom={
        'bedrockAgentCoreGatewayId': GATEWAY_ID, 'bedrockAgentCoreTargetId': TARGET_ID,
        'bedrockAgentCoreToolName': 'subagents___research', **patch}))


def test_native_target_schema_and_project_scoped_subagent_result():
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import gateway_target, handle_delegation
    settings = DelegationSettings.model_validate(CONFIG)
    request = gateway_target(settings)
    model = botocore.session.get_session().get_service_model('bedrock-agentcore-control')
    shape = model.operation_model('CreateGatewayTarget').input_shape
    validate_parameters(request, shape)
    for field in ('gatewayIdentifier', 'name'):
        assert re.fullmatch(shape.members[field].metadata['pattern'], request[field])
    assert request['targetConfiguration']['mcp']['lambda']['toolSchema']['inlinePayload'][0]['name'] == 'research'
    calls = []
    class Harness:
        def invoke(self, task, messages):
            calls.append((task, messages))
            return HarnessReply('Verified findings', 'end_turn')
    result = handle_delegation({'request': 'Find the prior failure'}, context(), settings, TARGET_ID, Control(), Harness())
    assert result['response'] == 'Verified findings' and result['completed'] is True
    task, messages = calls[0]
    assert task.context.project_id == 'payments' and task.harness_version == '2'
    assert task.context.actor_id == 'agent:payments:research'
    assert messages[0]['content'][0] == {'text': 'Find the prior failure'}
    assert result['task_id'] == task.id and task.id in messages[0]['content'][1]['text']


@pytest.mark.parametrize('name', ['sub_agents', 'sub--agents'])
def test_target_names_rejected_by_aws_fail_configuration_validation(name):
    from daeri.config.delegation import DelegationSettings
    with pytest.raises(ValueError):
        DelegationSettings.model_validate({**CONFIG, 'target_name': name})


@pytest.mark.parametrize('patch,event', [
    ({'bedrockAgentCoreGatewayId': 'different'}, {'request': 'Do work'}),
    ({'bedrockAgentCoreTargetId': 'different'}, {'request': 'Do work'}),
    ({'bedrockAgentCoreToolName': 'subagents___unknown'}, {'request': 'Do work'}),
    ({}, {'request': 'Do work', 'project_id': 'another'}),
])
def test_untrusted_gateway_scope_or_model_supplied_routing_is_rejected(patch, event):
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import handle_delegation
    class Harness:
        def invoke(self, *args):
            pytest.fail('Untrusted routing invoked a subagent')
    with pytest.raises(StoreError):
        handle_delegation(event, context(**patch), DelegationSettings.model_validate(CONFIG),
                          TARGET_ID, Control(), Harness())


def test_lambda_reports_non_utf8_delegation_as_invalid_input(monkeypatch):
    from daeri.infrastructure.delegation import lambda_handler

    def unexpected_call(**request):
        pytest.fail('Invalid input reached an AWS operation')

    monkeypatch.setenv('DAERI_DELEGATION_CONFIG', json.dumps({**CONFIG, 'target_id': TARGET_ID}))
    monkeypatch.setattr('boto3.client', lambda *args, **kwargs: SimpleNamespace(
        close=lambda: None, get_harness_endpoint=unexpected_call, invoke_harness=unexpected_call))
    result = lambda_handler({'request': '\ud800'}, context())
    assert result['completed'] is False and result['error'] == 'INVALID_INPUT'


def test_changed_release_and_inline_input_are_not_reported_as_completed():
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import handle_delegation
    class Harness:
        def invoke(self, *args):
            return HarnessReply('Need a decision', 'tool_use', ({'toolUseId': 'pending'},))
    result = handle_delegation({'request': 'Investigate'}, context(),
        DelegationSettings.model_validate(CONFIG), TARGET_ID, Control(), Harness())
    assert not result['completed'] and result['error'] == 'SUBAGENT_INPUT_REQUIRED'
    class Drift(Control):
        def get_harness_endpoint(self, **request):
            return {'endpoint': {'status': 'READY', 'liveVersion': '3', 'targetVersion': '3'}}
    with pytest.raises(StoreError, match='HARNESS_NOT_READY'):
        handle_delegation({'request': 'Investigate'}, context(),
            DelegationSettings.model_validate(CONFIG), TARGET_ID, Drift(), Harness())


def test_registration_command_uses_only_gateway_and_lambda_clients(monkeypatch, capsys):
    from scripts import configure_subagents as command

    project = SimpleNamespace(project_id='payments', approval_hook_arn='approval', region='us-east-1')
    monkeypatch.setattr(command.ProjectSettings, 'from_yaml', lambda path: project)
    monkeypatch.setattr(command, 'read_yaml', lambda path: CONFIG)
    monkeypatch.setattr(sys, 'argv', ['configure_subagents', '--config', 'subagents.yaml',
                                     '--project-config', 'project.yaml', '--apply'])

    @contextmanager
    def client(service, **options):
        assert service in {'bedrock-agentcore-control', 'lambda'}
        yield service

    def register(control, lambdas, settings, project_settings, check_harness):
        assert (control, lambdas, settings.project_id) == ('bedrock-agentcore-control', 'lambda', 'payments')
        assert project_settings is project and callable(check_harness)
        return {'target_id': TARGET_ID, 'project_id': 'payments'}

    monkeypatch.setattr('boto3.client', client)
    monkeypatch.setattr(command, 'configure_subagents', register)
    command.main()
    assert json.loads(capsys.readouterr().out) == {'target_id': TARGET_ID, 'project_id': 'payments'}


@pytest.fixture
def registration(monkeypatch):
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import configure_subagents
    from daeri.infrastructure import project_deployment

    project = SimpleNamespace(project_id='payments', approval_hook_arn=LAMBDA.replace('delegation', 'approval'))
    release = Project('payments', 'Payments', ROOT, 'RELEASE_V3', '3')
    orchestrator = {'memory': {'agentCoreMemoryConfiguration': {'arn': MEMORY}}, 'tools': [
        {'type': 'agentcore_gateway', 'config': {'agentCoreGateway': {'gatewayArn': GATEWAY}}}]}

    def verify(control, settings, check_harness):
        assert settings is project and callable(check_harness)
        return release, orchestrator

    def configure(control, functions, **patch):
        settings = DelegationSettings.model_validate({**CONFIG, **patch})
        return configure_subagents(control, functions, settings, project, lambda route: None)

    monkeypatch.setattr(project_deployment, 'verify_project', verify)
    return configure, orchestrator, project


class RegistrationControl(Control, PaginatedClient):
    aws_service = 'bedrock-agentcore-control'

    def __init__(self, *pages):
        self.pages = iter(pages)
        self.listed, self.writes = [], []
        self.child = {'memory': {'agentCoreMemoryConfiguration': {'arn': MEMORY}},
                      'status': 'READY', 'timeoutSeconds': 60}

    def get_harness(self, **request):
        assert request == {'harnessId': ARN.rsplit('/', 1)[1], 'harnessVersion': '2'}
        return {'harness': deepcopy(self.child)}

    def list_gateway_targets(self, **request):
        assert request['gatewayIdentifier'] == GATEWAY_ID
        self.listed.append(request.get('nextToken'))
        return next(self.pages)

    def create_gateway_target(self, **request):
        self.writes.append('CreateGatewayTarget')
        return {'targetId': TARGET_ID}

    def update_gateway_target(self, **request):
        self.writes.append('UpdateGatewayTarget')
        return {'targetId': TARGET_ID}

    def get_gateway_target(self, **request):
        return {'status': 'READY', 'targetConfiguration': {'mcp': {'lambda': {'lambdaArn': LAMBDA}}}}


class LambdaRegistration:
    def __init__(self, existing='retained'):
        self.existing, self.saved = existing, None

    def get_function_configuration(self, **request):
        return {'Timeout': 300, 'Environment': {'Variables': {'EXISTING': self.existing}}}

    def update_function_configuration(self, **request):
        model = botocore.session.get_session().get_service_model('lambda')
        validate_parameters(request, model.operation_model('UpdateFunctionConfiguration').input_shape)
        self.saved = request

    def get_waiter(self, name):
        return SimpleNamespace(wait=lambda **request: None)


@pytest.mark.parametrize('existing', [False, True])
def test_registration_uses_pinned_gateway_and_configures_scoped_lambda(registration, existing):
    configure, _, _ = registration
    items = [{'name': 'subagents', 'targetId': TARGET_ID}] if existing else []
    control = RegistrationControl({'items': [], 'nextToken': 'page-2'}, {'items': items})
    functions = LambdaRegistration()

    assert configure(control, functions) == {'target_id': TARGET_ID, 'project_id': 'payments'}
    assert control.listed == [None, 'page-2']
    assert control.writes == ['UpdateGatewayTarget' if existing else 'CreateGatewayTarget']
    variables = functions.saved['Environment']['Variables']
    assert variables['EXISTING'] == 'retained'
    saved = json.loads(variables['DAERI_DELEGATION_CONFIG'])
    assert saved['target_id'] == TARGET_ID and saved['project_id'] == 'payments'


def test_registration_rejects_a_nonconsecutive_repeated_cursor(registration):
    configure, _, _ = registration
    control = RegistrationControl(*({'items': [], 'nextToken': token} for token in ('page-2', 'page-3', 'page-2')),
                                  {'items': []})
    functions = LambdaRegistration()
    with pytest.raises(PaginationError):
        configure(control, functions)
    assert control.listed == [None, 'page-2', 'page-3']
    assert not control.writes and functions.saved is None


@pytest.mark.parametrize('failure', ['project_id', 'gateway', 'child_memory', 'child_hook',
                                    'child_timeout', 'environment_limit'])
def test_registration_preflight_rejects_incompatible_execution_before_writes(registration, failure):
    configure, orchestrator, project = registration
    control = RegistrationControl()
    functions = LambdaRegistration('x' * 4000 if failure == 'environment_limit' else 'retained')
    if failure == 'gateway':
        orchestrator['tools'] = []
    elif failure == 'child_memory':
        control.child['memory'] = {'disabled': {}}
    elif failure == 'child_hook':
        control.child['hooks'] = [{'beforeToolCall': {'target': {'lambda': {'arn': project.approval_hook_arn}}}}]
    elif failure == 'child_timeout':
        control.child['timeoutSeconds'] = 300
    project_id = 'foreign' if failure == 'project_id' else project.project_id
    with pytest.raises(StoreError) as error:
        configure(control, functions, project_id=project_id)
    assert error.value.code == ('INVALID_INPUT' if failure in {'project_id', 'environment_limit'} else 'PROJECT_NOT_READY')
    assert not control.listed and not control.writes and functions.saved is None
