"""Declare project resources once and publish only verified releases."""

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import re

import botocore.session
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
from pydantic import ValidationError
import pytest
from yaml import SafeLoader, load, safe_dump

from daeri.config.project import ProjectSettings
from daeri.config.server import HarnessReadiness
from daeri.core.errors import StoreError
from daeri.infrastructure.project_deployment import ProjectDeployment


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/ProjectAgent-abcdefghij'
HOOK = 'arn:aws:lambda:us-east-1:123456789012:function:approval'
MEMORY_ID = 'ProjectAgent_Memory-0123456789'
MEMORY_ARN = ARN.split(':harness/')[0] + ':memory/' + MEMORY_ID
DEPLOYMENT = {'project_id': 'payments', 'name': 'Payments', 'harness_arn': ARN,
              'harness_version': '4', 'approval_hook_arn': HOOK, 'memory_id': MEMORY_ID}
CATALOG = {'name': 'Payments', 'harness_arn': ARN, 'harness_endpoint': 'RELEASE_V4', 'harness_version': '4'}


class TemplateLoader(SafeLoader):
    pass


def _intrinsic(loader, tag, node):
    if tag in {'Ref', 'Sub', 'GetAtt'}:
        return {tag: loader.construct_scalar(node)}
    return {tag: loader.construct_sequence(node, deep=True)}


TemplateLoader.add_multi_constructor('!', _intrinsic)
DEPLOY_DIR = Path(__file__).resolve().parents[1] / 'deploy'
TEMPLATE = load((DEPLOY_DIR / 'project.yaml').read_text(), Loader=TemplateLoader)


def _api_fields(value):
    return {key[:1].lower() + key[1:]: _api_fields(item) for key, item in value.items()} if isinstance(value, dict) else value


def memory_strategies():
    return [_api_fields(item) for item in TEMPLATE['Resources']['SharedMemory']['Properties']['MemoryStrategies']]


def _memory():
    strategies = []
    for index, strategy in enumerate(memory_strategies()):
        key, fields = next(iter(strategy.items()))
        kind = {'semanticMemoryStrategy': 'SEMANTIC', 'episodicMemoryStrategy': 'EPISODIC',
                'summaryMemoryStrategy': 'SUMMARIZATION'}[key]
        strategy = {key: value for key, value in fields.items() if key != 'reflectionConfiguration'}
        strategy.update(type=kind, status='ACTIVE', strategyId=f'Strategy{index}-abcdefghij')
        if kind == 'EPISODIC':
            strategy['configuration'] = {'reflection': {
                'episodicReflectionConfiguration': fields['reflectionConfiguration']}}
        strategies.append(strategy)
    return {'id': MEMORY_ID, 'arn': MEMORY_ARN, 'name': MEMORY_ID.rsplit('-', 1)[0], 'status': 'ACTIVE',
            'eventExpiryDuration': 365, 'strategies': strategies}


class Control:
    def __init__(self):
        self.calls = []
        self.schema = botocore.session.get_session().get_service_model('bedrock-agentcore-control')
        self.memory = _memory()
        self.tags = {'daeri:project': 'payments', 'daeri:catalog': '/daeri/projects/'}
        self.harness = {'arn': ARN, 'status': 'READY', 'harnessVersion': '4',
                        'memory': {'agentCoreMemoryConfiguration': {'arn': MEMORY_ARN}},
                        'hooks': [{'beforeToolCall': {'target': {'lambda': {'arn': HOOK, 'failureMode': 'deny'}}}}],
                        'tools': [{'name': 'confirm_plan', 'type': 'inline_function', 'config': {'inlineFunction': {
                            'inputSchema': {'type': 'object', 'properties': {'plan': {'type': 'string'}},
                                            'required': ['plan']}}}}]}
        self.endpoint = {'harnessId': ARN.rsplit('/', 1)[1], 'endpointName': 'RELEASE_V4',
                         'status': 'READY', 'targetVersion': '4', 'liveVersion': '4'}

    def read(self, operation, request, key, value):
        validate_parameters(request, self.schema.operation_model(operation).input_shape)
        self.calls.append((operation, request))
        return {key: deepcopy(value)}

    def get_memory(self, **request):
        return self.read('GetMemory', request, 'memory', self.memory)

    def list_tags_for_resource(self, **request):
        return self.read('ListTagsForResource', request, 'tags', self.tags)

    def get_harness(self, **request):
        return self.read('GetHarness', request, 'harness', self.harness)

    def get_harness_endpoint(self, **request):
        return self.read('GetHarnessEndpoint', request, 'endpoint', self.endpoint)


class Ssm:
    def __init__(self):
        self.value = None
        self.writes = []

    def get_parameter(self, **request):
        if self.value is None:
            raise ClientError({'Error': {'Code': 'ParameterNotFound'}}, 'GetParameter')
        return {'Parameter': {'Value': self.value}}

    def put_parameter(self, **request):
        self.writes.append(request)
        self.value = request['Value']


def settings(**changes):
    return ProjectSettings.model_validate({**DEPLOYMENT, **changes})


def deployer():
    control, ssm = Control(), Ssm()
    return ProjectDeployment(control, ssm, HarnessReadiness(control, HOOK).check), control, ssm


def test_publish_only_writes_catalog_for_a_release_created_by_cloudformation():
    deployment, control, ssm = deployer()

    assert deployment.apply(settings()) == CATALOG

    assert json.loads(ssm.value) == CATALOG
    assert ssm.writes[0]['Name'] == '/daeri/projects/payments'
    assert [operation for operation, _ in control.calls] == [
        'GetHarnessEndpoint', 'GetHarness', 'GetMemory', 'ListTagsForResource']
    assert control.calls[1][1]['harnessVersion'] == '4'


def test_check_uses_exact_declared_and_published_release_without_writes():
    deployment, control, ssm = deployer()
    ssm.value = json.dumps(CATALOG)
    assert deployment.check(settings()) == CATALOG
    assert control.calls[1][1]['harnessVersion'] == '4'
    ssm.value = json.dumps({**CATALOG, 'harness_endpoint': 'RELEASE_V3', 'harness_version': '3'})
    with pytest.raises(StoreError, match='PROJECT_NOT_READY'):
        deployment.check(settings())
    assert not ssm.writes


@pytest.mark.parametrize('change', ['owner', 'namespace', 'expiry', 'reflection', 'managed', 'name', 'arn',
                                     'binding', 'actor', 'retrieval'])
def test_publish_rejects_memory_that_breaks_project_contract(change):
    deployment, control, ssm = deployer()
    binding = control.harness['memory']['agentCoreMemoryConfiguration']
    if change == 'owner':
        control.tags['daeri:project'] = 'other-project'
    elif change == 'namespace':
        control.memory['strategies'][0]['namespaces'] = ['/facts/{actorId}/']
    elif change == 'expiry':
        control.memory['strategies'][0]['recordExpiryDurationDays'] = 60
    elif change == 'reflection':
        control.memory['strategies'][1]['configuration']['reflection'] = {}
    elif change == 'managed':
        control.memory['managedByResourceArn'] = ARN
    elif change == 'name':
        control.memory['name'] = 'Unexpected'
    elif change == 'arn':
        control.memory['arn'] = MEMORY_ARN.replace(':us-east-1:', ':eu-west-1:')
    elif change == 'binding':
        binding['arn'] = MEMORY_ARN.replace('ProjectAgent', 'Other')
    elif change == 'actor':
        binding['actorId'] = 'shared-fake-user'
    elif change == 'retrieval':
        binding['retrievalConfig'] = {'/shared/facts/': {'strategyId': 'Wrong'}}
    with pytest.raises(StoreError, match='PROJECT_NOT_READY'):
        deployment.apply(settings())
    assert not ssm.writes


@pytest.mark.parametrize('change', ['drift', 'hook', 'status'])
def test_unready_release_leaves_published_project_untouched(change):
    deployment, control, ssm = deployer()
    ssm.value = json.dumps({**CATALOG, 'harness_endpoint': 'RELEASE_V3', 'harness_version': '3'})
    before = ssm.value
    if change == 'drift':
        control.endpoint['targetVersion'] = '5'
    elif change == 'hook':
        control.harness['hooks'].clear()
    else:
        control.endpoint['status'] = 'CREATING'
    with pytest.raises(StoreError, match='HARNESS_NOT_READY'):
        deployment.apply(settings())
    assert not ssm.writes and ssm.value == before


def test_template_retains_previous_release_and_outputs_the_publisher_input():
    resources = TEMPLATE['Resources']
    for name in ('SharedMemory', 'Orchestrator', 'Release'):
        assert resources[name]['DeletionPolicy'] == resources[name]['UpdateReplacePolicy'] == 'Retain'
    assert resources['Release']['Properties'] == {
        'EndpointName': {'Sub': 'RELEASE_V${Orchestrator.Version}'},
        'HarnessId': {'GetAtt': 'Orchestrator.HarnessId'},
        'TargetVersion': {'GetAtt': 'Orchestrator.Version'},
    }
    assert resources['SharedMemory']['Properties']['EventExpiryDuration'] == 365
    policy = json.loads((DEPLOY_DIR / 'project-stack-policy.json').read_text())
    assert {'Effect': 'Deny', 'Action': 'Update:Modify', 'Principal': '*',
            'Resource': 'LogicalResourceId/SharedMemory'} in policy['Statement']
    assert 'AWS::SSM::Parameter' not in {item['Type'] for item in resources.values()}
    output = TEMPLATE['Outputs']['ProjectConfiguration']['Value']['Fn::ToJsonString']
    assert set(output) == set(DEPLOYMENT) | {'region', 'project_parameter_prefix'}
    assert output['harness_version'] == {'GetAtt': 'Orchestrator.Version'}
    harness = resources['Orchestrator']['Properties']
    assert harness['Hooks'][0]['BeforeToolCall']['Target']['Lambda'] == {
        'Arn': {'Ref': 'ApprovalHookArn'}, 'FailureMode': 'deny'}
    tool = next(tool for tool in harness['Tools'] if tool['Name'] == 'confirm_plan')
    assert tool['Type'] == 'inline_function'
    assert tool['Config']['InlineFunction']['InputSchema']['required'] == ['plan']


def test_stack_catalog_prefix_is_canonical_for_memory_tags_and_publication():
    pattern = TEMPLATE['Parameters']['ProjectCatalogPrefix']['AllowedPattern']
    assert re.fullmatch(pattern, '/custom/projects/')
    assert not any(re.fullmatch(pattern, value) for value in ('/custom/projects', '/custom//', '/custom/../'))


def test_invalid_project_prefix_is_a_field_validation_error():
    with pytest.raises(ValidationError) as invalid:
        settings(project_parameter_prefix='relative/projects/')
    assert invalid.value.errors(include_input=False)[0]['loc'] == ('project_parameter_prefix',)


@pytest.mark.parametrize('change', [{'approval_hook_arn': 'arn:aws:sns:us-east-1:123456789012:private-topic'},
                                    {'memory_id': 'invalid/private-value'},
                                    {'harness_version': 'private-value'},
                                    {'api_key': 'private-value'}])
def test_project_command_rejects_invalid_output_before_aws_clients(tmp_path, monkeypatch, capsys, change):
    from scripts.configure_project import main

    monkeypatch.setattr('scripts.configure_project.boto3.Session', lambda: pytest.fail('Unexpected AWS client'))
    config = tmp_path / 'project.json'
    config.write_text(safe_dump({**DEPLOYMENT, **change}))
    assert main(['--config', str(config), '--apply']) == 2
    output = capsys.readouterr().out
    assert json.loads(output)['error']['code'] == 'INVALID_INPUT'
    assert 'private-' not in output


def test_project_command_publishes_cloudformation_json_output(tmp_path, monkeypatch, capsys):
    from scripts.configure_project import main

    control, ssm = Control(), Ssm()

    class Session:
        def client(self, service, *, region_name, config):
            assert region_name == 'us-east-1'
            return nullcontext({'bedrock-agentcore-control': control, 'ssm': ssm}[service])

    monkeypatch.setattr('scripts.configure_project.boto3.Session', Session)
    config = tmp_path / 'project.json'
    config.write_text(json.dumps({**DEPLOYMENT, 'region': 'us-east-1'}))

    assert main(['--config', str(config), '--apply']) == 0
    assert json.loads(capsys.readouterr().out) == {'project': 'payments', 'deployment': CATALOG}
    assert json.loads(ssm.value) == CATALOG
