"""Project publishing verifies shared memory before exposing a release."""

from copy import deepcopy
import json

import botocore.session
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
import pytest

from daeri.config.project import ProjectSettings
from daeri.core.errors import StoreError
from daeri.infrastructure.project_deployment import ProjectDeployment, memory_strategies


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/ProjectAgent-abcdefghij'
HOOK = 'arn:aws:lambda:us-east-1:123456789012:function:approval'
MODEL = {'bedrockModelConfig': {'modelId': 'example-model'}}


def settings(**changes):
    return ProjectSettings(project_id='payments', name='Payments', harness_arn=ARN,
                           approval_hook_arn=HOOK, **changes)


def missing(operation):
    return ClientError({'Error': {'Code': 'ResourceNotFoundException'}}, operation)


class Control:
    def __init__(self):
        self.calls = []
        self.memory = None
        self.tags = {}
        self.endpoint = None
        self.harness = {'arn': ARN, 'harnessId': ARN.rsplit('/', 1)[1], 'status': 'READY',
                        'harnessVersion': '1', 'model': deepcopy(MODEL),
                        'memory': {'disabled': {}}, 'hooks': [{'original': 'hook'}],
                        'tools': [{'original': 'tool'}]}
        self.schema = botocore.session.get_session().get_service_model('bedrock-agentcore-control')

    def record(self, operation, request):
        validate_parameters(request, self.schema.operation_model(operation).input_shape)
        self.calls.append((operation, deepcopy(request)))

    def list_memories(self, **request):
        self.record('ListMemories', request)
        return {'memories': [] if self.memory is None else [{'id': self.memory['id']}]}

    def create_memory(self, **request):
        self.record('CreateMemory', request)
        self.tags = request['tags']
        strategies = []
        for index, source in enumerate(request['memoryStrategies']):
            key, value = next(iter(source.items()))
            kind = {'semanticMemoryStrategy': 'SEMANTIC', 'episodicMemoryStrategy': 'EPISODIC',
                    'summaryMemoryStrategy': 'SUMMARIZATION'}[key]
            item = {key: deepcopy(value) for key, value in value.items() if key != 'reflectionConfiguration'}
            item.update(type=kind, status='ACTIVE', strategyId=f'Strategy{index}-abcdefghij')
            if kind == 'EPISODIC':
                item['configuration'] = {'reflection': {
                    'episodicReflectionConfiguration': value['reflectionConfiguration']}}
            strategies.append(item)
        self.memory = {'id': request['name'] + '-abcdefghij', 'name': request['name'], 'status': 'ACTIVE',
                       'arn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/' + request['name'] + '-abcdefghij',
                       'strategies': strategies, 'eventExpiryDuration': request['eventExpiryDuration']}
        for key in ('memoryExecutionRoleArn', 'encryptionKeyArn'):
            if key in request:
                self.memory[key] = request[key]
        return {'memory': deepcopy(self.memory)}

    def get_memory(self, **request):
        self.record('GetMemory', request)
        return {'memory': deepcopy(self.memory)}

    def list_tags_for_resource(self, **request):
        self.record('ListTagsForResource', request)
        return {'tags': deepcopy(self.tags)}

    def get_harness(self, **request):
        self.record('GetHarness', request)
        return {'harness': deepcopy(self.harness)}

    def update_harness(self, **request):
        self.record('UpdateHarness', request)
        self.harness['memory'] = deepcopy(request['memory']['optionalValue'])
        self.harness['harnessVersion'] = '2'
        return {'harness': deepcopy(self.harness)}

    def get_harness_endpoint(self, **request):
        self.record('GetHarnessEndpoint', request)
        if self.endpoint is None:
            raise missing('GetHarnessEndpoint')
        return {'endpoint': deepcopy(self.endpoint)}

    def create_harness_endpoint(self, **request):
        self.record('CreateHarnessEndpoint', request)
        self.endpoint = {**request, 'status': 'READY', 'liveVersion': request['targetVersion']}
        return {'endpoint': deepcopy(self.endpoint)}


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


def deployer(control=None, ssm=None, readiness=None):
    control, ssm = control or Control(), ssm or Ssm()
    checked = []
    ready = readiness or checked.append
    return ProjectDeployment(control, ssm, ready), control, ssm, checked


def test_apply_creates_independent_memory_and_publishes_verified_version_last():
    deployment, control, ssm, checked = deployer()
    result = deployment.apply(settings())
    assert result == {'name': 'Payments', 'harness_arn': ARN,
                      'harness_endpoint': 'RELEASE_V2', 'harness_version': '2'}
    assert json.loads(ssm.value) == result
    assert checked[-1].harness_version == '2'
    assert control.harness['model'] == MODEL
    assert control.harness['hooks'] == [{'original': 'hook'}]
    assert control.harness['tools'] == [{'original': 'tool'}]
    create = next(request for operation, request in control.calls if operation == 'CreateMemory')
    assert create['eventExpiryDuration'] == 365
    assert create['memoryStrategies'] == memory_strategies()
    assert 'recordExpirySpecification' not in json.dumps(create)
    assert len(ssm.writes) == 1


def test_reapply_preserves_memory_and_release_without_creating_harness_version():
    deployment, control, ssm, _ = deployer()
    deployment.apply(settings())
    control.calls.clear()
    deployment.apply(settings())
    assert not {'CreateMemory', 'UpdateMemory', 'UpdateHarness', 'CreateHarnessEndpoint'} & {
        operation for operation, _ in control.calls}


def test_apply_preserves_an_existing_different_byo_memory_until_its_migration_is_resolved():
    deployment, control, ssm, _ = deployer()
    original = {'agentCoreMemoryConfiguration': {'arn':
        'arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/ExistingProject-abcdefghij'}}
    control.harness['memory'] = deepcopy(original)
    with pytest.raises(StoreError, match='PROJECT_NOT_READY'):
        deployment.apply(settings())
    assert control.harness['memory'] == original and not ssm.writes
    assert not any(operation.startswith(('Create', 'Update')) for operation, _ in control.calls)


def test_check_uses_published_version_and_performs_no_writes():
    deployment, control, ssm, _ = deployer()
    deployment.apply(settings())
    control.calls.clear()
    ssm.writes.clear()
    deployment.check(settings())
    assert ('GetHarness', {'harnessId': ARN.rsplit('/', 1)[1], 'harnessVersion': '2'}) in control.calls
    assert not ssm.writes
    assert all(not operation.startswith(('Create', 'Update')) for operation, _ in control.calls)


@pytest.mark.parametrize('change', ['disabled', 'actor', 'namespace', 'expiry', 'reflection', 'owner'])
def test_check_rejects_memory_that_breaks_shared_project_contract(change):
    deployment, control, _, _ = deployer()
    deployment.apply(settings())
    if change == 'disabled':
        control.harness['memory'] = {'disabled': {}}
    elif change == 'actor':
        control.harness['memory']['agentCoreMemoryConfiguration']['actorId'] = 'shared-fake-user'
    elif change == 'namespace':
        control.memory['strategies'][0]['namespaces'] = ['/facts/{actorId}/']
    elif change == 'expiry':
        control.memory['strategies'][0]['recordExpiryDurationDays'] = 60
    elif change == 'reflection':
        control.memory['strategies'][1]['configuration']['reflection'] = {}
    else:
        control.tags['daeri:project'] = 'other-project'
    with pytest.raises(StoreError, match='project'):
        deployment.check(settings())


def test_existing_memory_with_wrong_owner_is_left_untouched():
    deployment, control, ssm, _ = deployer()
    deployment.apply(settings())
    control.tags['daeri:project'] = 'other-project'
    control.calls.clear()
    ssm.writes.clear()
    with pytest.raises(StoreError):
        deployment.apply(settings())
    assert not ssm.writes
    assert all(not operation.startswith(('Create', 'Update')) for operation, _ in control.calls)


def test_release_endpoint_drift_fails_before_project_publication():
    deployment, control, ssm, _ = deployer()
    deployment.apply(settings())
    control.endpoint['liveVersion'] = '3'
    ssm.writes.clear()
    with pytest.raises(StoreError):
        deployment.apply(settings())
    assert not ssm.writes


def test_failed_readiness_never_publishes_project():
    def unavailable(route):
        raise StoreError('HARNESS_NOT_READY', 'Invalid approval hook')

    deployment, _, ssm, _ = deployer(readiness=unavailable)
    with pytest.raises(StoreError):
        deployment.apply(settings())
    assert not ssm.writes


def test_harness_update_waits_for_returned_immutable_version():
    deployment, control, _, _ = deployer()
    deployment.apply(settings())
    update = next(index for index, (operation, _) in enumerate(control.calls) if operation == 'UpdateHarness')
    reads = [request for operation, request in control.calls[update + 1:] if operation == 'GetHarness']
    assert reads[0]['harnessVersion'] == '2'


def test_pending_memory_stops_without_publishing_and_can_be_retried(monkeypatch):
    deployment, control, ssm, _ = deployer()
    create = control.create_memory

    def pending(**request):
        result = create(**request)
        control.memory['status'] = 'CREATING'
        return result

    control.create_memory = pending
    monkeypatch.setattr('daeri.infrastructure.project_deployment.time.sleep', lambda seconds: None)
    with pytest.raises(StoreError, match='resource is not ready'):
        deployment.apply(settings())
    assert not ssm.writes
    control.memory['status'] = 'ACTIVE'
    deployment.apply(settings())
    assert len([request for operation, request in control.calls if operation == 'CreateMemory']) == 1


def test_named_resources_and_secrets_are_validated_before_any_client(tmp_path):
    path = tmp_path / 'project.yaml'
    path.write_text('project_id: payments\nname: Payments\nharness_arn: invalid\napproval_hook_arn: arn:lambda\napi_key: secret-value\n')
    with pytest.raises(StoreError) as error:
        ProjectSettings.from_yaml(path)
    assert 'secret-value' not in str(error.value)


def test_project_memory_identity_is_stable_and_separate_by_catalogue():
    assert settings().memory_name == settings().memory_name
    assert settings().memory_name != settings(project_parameter_prefix='/other/projects/').memory_name


def test_memory_uses_operator_selected_role_and_encryption_key():
    deployment, control, _, _ = deployer()
    selected = settings(memory_execution_role_arn='arn:aws:iam::123456789012:role/Memory',
                        encryption_key_arn='arn:aws:kms:us-east-1:123456789012:key/example')
    deployment.apply(selected)
    assert control.memory['memoryExecutionRoleArn'] == selected.memory_execution_role_arn
    assert control.memory['encryptionKeyArn'] == selected.encryption_key_arn
    control.memory['encryptionKeyArn'] = 'other'
    with pytest.raises(StoreError):
        deployment.check(selected)


def test_project_command_validates_input_before_aws_clients(tmp_path, monkeypatch, capsys):
    from scripts.configure_project import main

    def clients_are_not_needed():
        raise AssertionError('AWS should not be initialized for invalid operator input')

    monkeypatch.setattr('scripts.configure_project.boto3.Session', clients_are_not_needed)
    config = tmp_path / 'invalid.yaml'
    config.write_text('project_id: payments\napi_key: should-stay-private\n')
    assert main(['--config', str(config)]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)['error']['code'] == 'INVALID_INPUT'
    assert 'should-stay-private' not in output


def restored_memory(control):
    selected = 'RestoredProject-0123456789'
    request = {'name': selected.rsplit('-', 1)[0], 'eventExpiryDuration': 365,
               'memoryStrategies': memory_strategies(),
               'tags': {'daeri:project': 'payments', 'daeri:catalog': '/daeri/projects/'}}
    control.create_memory(**request)
    control.memory['id'] = selected
    control.memory['arn'] = control.memory['arn'].rsplit('/', 1)[0] + '/' + selected
    return selected


def test_explicit_restored_memory_can_be_verified_and_published_without_creating_another_memory():
    deployment, control, ssm, _ = deployer()
    target = restored_memory(control)
    control.harness['memory'] = {'agentCoreMemoryConfiguration': {'arn':
        'arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/Original-abcdefghij'}}
    control.calls.clear()
    selected = settings(memory_id=target)
    deployment.apply(selected)
    assert control.harness['memory']['agentCoreMemoryConfiguration']['arn'] == control.memory['arn']
    assert ssm.writes
    assert not any(operation in {'ListMemories', 'CreateMemory', 'UpdateMemory'} for operation, _ in control.calls)
    control.calls.clear()
    ssm.writes.clear()
    deployment.check(selected)
    assert not ssm.writes
    assert all(not operation.startswith(('Create', 'Update')) for operation, _ in control.calls)


@pytest.mark.parametrize('change', ['owner', 'namespace', 'expiry', 'managed', 'name'])
def test_invalid_explicit_memory_is_not_attached_or_published(change):
    deployment, control, ssm, _ = deployer()
    target = restored_memory(control)
    if change == 'owner':
        control.tags['daeri:project'] = 'other'
    elif change == 'namespace':
        control.memory['strategies'][0]['namespaces'] = ['/private/{actorId}/']
    elif change == 'expiry':
        control.memory['eventExpiryDuration'] = 30
    elif change == 'managed':
        control.memory['managedByResourceArn'] = ARN
    else:
        control.memory['name'] = 'Unexpected'
    control.calls.clear()
    with pytest.raises(StoreError, match='PROJECT_NOT_READY'):
        deployment.apply(settings(memory_id=target))
    assert not ssm.writes
    assert not any(operation.startswith(('Create', 'Update')) for operation, _ in control.calls)


def test_memory_override_is_validated_before_aws_initialization(tmp_path, monkeypatch, capsys):
    from scripts.configure_project import main
    monkeypatch.setattr('scripts.configure_project.boto3.Session', lambda: pytest.fail('Unexpected AWS client'))
    config = tmp_path / 'invalid.yaml'
    config.write_text(f'project_id: payments\nname: Payments\nharness_arn: {ARN}\n'
                      f'approval_hook_arn: {HOOK}\nmemory_id: invalid/private-value\n')
    assert main(['--config', str(config), '--apply']) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'INVALID_INPUT'


@pytest.mark.parametrize('apply', [False, True])
def test_project_command_rejects_non_lambda_approval_hook_before_aws(tmp_path, monkeypatch, capsys, apply):
    from scripts.configure_project import main

    monkeypatch.setattr('scripts.configure_project.boto3.Session', lambda: pytest.fail('Invalid input created AWS clients'))
    config = tmp_path / 'project.yaml'
    config.write_text(f'project_id: payments\nname: Payments\nharness_arn: {ARN}\n'
                      'approval_hook_arn: arn:aws:sns:us-east-1:123456789012:private-topic\n')
    assert main(['--config', str(config)] + (['--apply'] if apply else [])) == 2
    output = capsys.readouterr().out
    assert json.loads(output)['error']['code'] == 'INVALID_INPUT'
    assert 'private-topic' not in output
