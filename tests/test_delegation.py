"""Managed subagents are fixed project targets behind the approved Gateway call."""

from copy import deepcopy
from types import SimpleNamespace

import botocore.session
from botocore.validate import validate_parameters
import pytest

from daeri.core.cloud import HarnessReply
from daeri.core.errors import StoreError


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/Research-abcdefghij'
GATEWAY = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/project-gateway'
LAMBDA = 'arn:aws:lambda:us-east-1:123456789012:function:delegation'
CONFIG = {'project_id': 'payments', 'gateway_arn': GATEWAY, 'target_name': 'subagents',
          'lambda_arn': LAMBDA, 'subagents': {'research': {'description': 'Research project facts',
          'harness_arn': ARN, 'harness_endpoint': 'RELEASE_V2', 'harness_version': '2'}}}


class Control:
    def get_harness_endpoint(self, **request):
        return {'endpoint': {'status': 'READY', 'liveVersion': '2', 'targetVersion': '2'}}


def context(**patch):
    return SimpleNamespace(client_context=SimpleNamespace(custom={
        'bedrockAgentCoreGatewayId': 'project-gateway', 'bedrockAgentCoreTargetId': 'target-1',
        'bedrockAgentCoreToolName': 'subagents___research', **patch}))


def test_native_target_schema_and_project_scoped_subagent_result():
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import gateway_target, handle_delegation
    settings = DelegationSettings.model_validate(CONFIG)
    request = gateway_target(settings)
    model = botocore.session.get_session().get_service_model('bedrock-agentcore-control')
    validate_parameters(request, model.operation_model('CreateGatewayTarget').input_shape)
    assert request['targetConfiguration']['mcp']['lambda']['toolSchema']['inlinePayload'][0]['name'] == 'research'
    calls = []
    class Harness:
        def invoke(self, task, messages):
            calls.append((task, messages))
            return HarnessReply('Verified findings', 'end_turn')
    result = handle_delegation({'request': 'Find the prior failure'}, context(), settings, 'target-1', Control(), Harness())
    assert result['response'] == 'Verified findings' and result['completed'] is True
    task, messages = calls[0]
    assert task.context.project_id == 'payments' and task.harness_version == '2'
    assert task.context.actor_id == 'agent:payments:research'
    assert messages[0]['content'][0] == {'text': 'Find the prior failure'}
    assert result['task_id'] == task.id and task.id in messages[0]['content'][1]['text']


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
                          'target-1', Control(), Harness())


def test_changed_release_and_inline_input_are_not_reported_as_completed():
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import handle_delegation
    class Harness:
        def invoke(self, *args):
            return HarnessReply('Need a decision', 'tool_use', ({'toolUseId': 'pending'},))
    result = handle_delegation({'request': 'Investigate'}, context(),
        DelegationSettings.model_validate(CONFIG), 'target-1', Control(), Harness())
    assert not result['completed'] and result['error'] == 'SUBAGENT_INPUT_REQUIRED'
    class Drift(Control):
        def get_harness_endpoint(self, **request):
            return {'endpoint': {'status': 'READY', 'liveVersion': '3', 'targetVersion': '3'}}
    with pytest.raises(StoreError, match='HARNESS_NOT_READY'):
        handle_delegation({'request': 'Investigate'}, context(),
            DelegationSettings.model_validate(CONFIG), 'target-1', Drift(), Harness())


def test_registration_connects_gateway_preserves_root_tools_and_publishes_the_new_release():
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import configure_subagents
    from test_project_deployment import Control as ProjectControl, Ssm, settings, missing
    from daeri.infrastructure.project_deployment import ProjectDeployment

    class DeploymentControl(ProjectControl):
        def get_harness(self, **request):
            if request['harnessId'] == ARN.rsplit('/', 1)[1]:
                self.record('GetHarness', request)
                return {'harness': {'status': 'READY', 'timeoutSeconds': 60, 'tools': [],
                    'memory': {'agentCoreMemoryConfiguration': {'arn': self.memory['arn']}}}}
            return super().get_harness(**request)
        def get_harness_endpoint(self, **request):
            if request['harnessId'] == ARN.rsplit('/', 1)[1]:
                self.record('GetHarnessEndpoint', request)
                return {'endpoint': {'status': 'READY', 'liveVersion': '2', 'targetVersion': '2'}}
            if self.endpoint is not None and self.endpoint['endpointName'] != request['endpointName']:
                self.record('GetHarnessEndpoint', request)
                raise missing('GetHarnessEndpoint')
            return super().get_harness_endpoint(**request)
        def update_harness(self, **request):
            if 'tools' in request:
                self.record('UpdateHarness', request)
                self.harness['tools'] = request['tools']
                self.harness['harnessVersion'] = '3'
                return {'harness': deepcopy(self.harness)}
            return super().update_harness(**request)
        def list_gateway_targets(self, **request):
            self.record('ListGatewayTargets', request)
            return {'items': []}
        def create_gateway_target(self, **request):
            self.record('CreateGatewayTarget', request)
            return {'targetId': 'target-1'}
        def get_gateway_target(self, **request):
            self.record('GetGatewayTarget', request)
            return {'status': 'READY'}

    class Lambdas:
        def get_function_configuration(self, **request):
            return {'Timeout': 300, 'Environment': {'Variables': {'EXISTING': 'retained'}}}
        def update_function_configuration(self, **request):
            model = botocore.session.get_session().get_service_model('lambda')
            validate_parameters(request, model.operation_model('UpdateFunctionConfiguration').input_shape)
            self.saved = request
        def get_waiter(self, name):
            return SimpleNamespace(wait=lambda **request: None)

    control, ssm, functions = DeploymentControl(), Ssm(), Lambdas()
    original_tool = {'name': 'analysis', 'type': 'agentcore_code_interpreter',
                     'config': {'agentCoreCodeInterpreter': {}}}
    control.harness['tools'] = [original_tool]
    project = settings()
    ProjectDeployment(control, ssm, lambda project: None).apply(project)
    result = configure_subagents(control, functions, ssm, DelegationSettings.model_validate(CONFIG),
                                 project, lambda project: None)
    assert result['project']['harness_version'] == '3'
    assert control.harness['tools'][0] == original_tool
    assert control.harness['tools'][-1]['config']['agentCoreGateway']['gatewayArn'] == GATEWAY
    assert functions.saved['Environment']['Variables']['EXISTING'] == 'retained'
    saved = __import__('json').loads(functions.saved['Environment']['Variables']['DAERI_DELEGATION_CONFIG'])
    assert saved['target_id'] == 'target-1' and saved['project_id'] == 'payments'


def test_registration_rejects_a_different_orchestrator_before_creating_resources():
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import configure_subagents
    from test_project_deployment import settings
    class Ssm:
        def get_parameter(self, **request):
            return {'Parameter': {'Value': __import__('json').dumps({
                'name': 'Payments', 'harness_arn': ARN, 'harness_endpoint': 'RELEASE_V2', 'harness_version': '2'})}}
    class Control:
        def get_harness(self, **request):
            pytest.fail('Read a different orchestrator instead of rejecting the mismatched project binding')
    with pytest.raises(StoreError, match='PROJECT_NOT_READY'):
        configure_subagents(Control(), None, Ssm(), DelegationSettings.model_validate(CONFIG),
                             settings(), lambda route: None)


@pytest.mark.parametrize('failure', ['draft_memory', 'child_hook', 'child_timeout', 'environment_limit'])
def test_registration_preflight_rejects_incompatible_execution_before_writes(failure):
    import json
    from daeri.config.delegation import DelegationSettings
    from daeri.infrastructure.delegation import configure_subagents
    from test_project_deployment import settings, ARN as ROOT_ARN
    project = settings()
    memory = {'agentCoreMemoryConfiguration': {'arn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/Project-abcdefghij'}}
    class Ssm:
        def get_parameter(self, **request):
            return {'Parameter': {'Value': json.dumps({'name': project.name, 'harness_arn': ROOT_ARN,
                'harness_endpoint': 'RELEASE_V2', 'harness_version': '2'})}}
    class Control:
        def get_harness(self, **request):
            harness = {'memory': deepcopy(memory), 'status': 'READY', 'tools': [], 'timeoutSeconds': 60}
            if request['harnessId'] == ARN.rsplit('/', 1)[1]:
                if failure == 'child_hook':
                    harness['hooks'] = [{'beforeToolCall': {'target': {'lambda': {'arn': project.approval_hook_arn}}}}]
                if failure == 'child_timeout':
                    harness['timeoutSeconds'] = 300
            elif failure == 'draft_memory' and 'harnessVersion' not in request:
                harness['memory'] = {'disabled': {}}
            return {'harness': harness}
        def get_harness_endpoint(self, **request):
            return {'endpoint': {'status': 'READY', 'liveVersion': '2', 'targetVersion': '2'}}
    class Lambdas:
        def get_function_configuration(self, **request):
            return {'Timeout': 300, 'Environment': {'Variables': {'EXISTING': 'x' * (4000 if failure == 'environment_limit' else 0)}}}
    with pytest.raises(StoreError):
        configure_subagents(Control(), Lambdas(), Ssm(), DelegationSettings.model_validate(CONFIG),
                            project, lambda route: None)
