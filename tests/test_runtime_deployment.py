"""The managed hosting boundary is configured without creating live AWS resources."""

import json

import pytest
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
import boto3
from pydantic import ValidationError

from daeri.config.runtime import RuntimeSettings
from daeri.config.connections import ProjectConnections
from daeri.infrastructure.runtime_deployment import configure_runtime, runtime_request
from daeri.core.errors import StoreError


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/daeri-1234567890'
BASE = {
    'name': 'daeri', 'image_uri': '123456789012.dkr.ecr.us-east-1.amazonaws.com/daeri:release',
    'role_arn': 'arn:aws:iam::123456789012:role/daeri-service', 'client_ids': ['public-plugin'],
    'server': {'storage': {'bucket': 'daeri-workspace'},
               'auth': {'issuer': 'https://login.example.com/company', 'audience': 'company-daeri'},
               'approval_hook_arn': 'arn:aws:lambda:us-east-1:123456789012:function:approval'},
}


class Control:
    def __init__(self, existing_endpoint=False):
        self.calls, self.existing_endpoint = [], existing_endpoint
        self.version = '1'
        self.endpoint_version = '1'
        self.metadata = None

    def create_agent_runtime(self, **request):
        self.calls.append(('create', request))
        return {'agentRuntimeId': 'daeri-1234567890'}

    def get_agent_runtime(self, **request):
        self.calls.append(('get', request))
        return {'agentRuntimeArn': ARN, 'status': 'READY', 'agentRuntimeVersion': self.version,
                'metadataConfiguration': self.metadata}

    def update_agent_runtime(self, **request):
        self.calls.append(('update', request))
        self.version = '2'
        self.metadata = request.get('metadataConfiguration')
        return {'agentRuntimeVersion': self.version, 'status': 'UPDATING'}

    def get_agent_runtime_endpoint(self, **request):
        if request['endpointName'] == 'DEFAULT':
            version = self.version
        elif self.existing_endpoint:
            version = self.endpoint_version
        else:
            raise ClientError({'Error': {'Code': 'ResourceNotFoundException'}}, 'GetAgentRuntimeEndpoint')
        return {'status': 'READY', 'liveVersion': version, 'targetVersion': version}

    def create_agent_runtime_endpoint(self, **request):
        self.calls.append(('create_endpoint', request))
        self.existing_endpoint = True
        self.endpoint_version = request['agentRuntimeVersion']

    def update_agent_runtime_endpoint(self, **request):
        self.calls.append(('update_endpoint', request))
        self.endpoint_version = request['agentRuntimeVersion']


def test_create_publishes_only_the_version_with_real_public_url_and_preserves_token_boundary():
    client = Control()
    settings = RuntimeSettings.model_validate(BASE)
    result = configure_runtime(client, settings)
    create = next(request for operation, request in client.calls if operation == 'create')
    assert create['environmentVariables'] == {}
    update = next(request for operation, request in client.calls if operation == 'update')
    assert 'metadataConfiguration' not in create
    assert update['metadataConfiguration'] == {'requireMMDSV2': True}
    config = json.loads(update['environmentVariables']['DAERI_SERVER_CONFIG'])
    assert config['public_url'] == result['public_url']
    assert result['public_url'].endswith('/invocations?qualifier=PROD')
    assert update['protocolConfiguration'] == {'serverProtocol': 'MCP'}
    assert update['requestHeaderConfiguration'] == {'requestHeaderAllowlist': ['Authorization']}
    assert update['authorizerConfiguration']['customJWTAuthorizer'] == {
        'discoveryUrl': 'https://login.example.com/company/.well-known/openid-configuration',
        'allowedAudience': ['company-daeri'], 'allowedClients': ['public-plugin'], 'allowedScopes': ['daeri/use'],
    }
    assert client.calls[-1] == ('create_endpoint', {
        'agentRuntimeId': 'daeri-1234567890', 'name': 'PROD', 'agentRuntimeVersion': '2'})
    model = boto3.Session()._session.get_service_model('bedrock-agentcore-control')
    validate_parameters(create, model.operation_model('CreateAgentRuntime').input_shape)
    validate_parameters(update, model.operation_model('UpdateAgentRuntime').input_shape)


def test_update_reuses_existing_runtime_and_endpoint():
    client = Control(existing_endpoint=True)
    configure_runtime(client, RuntimeSettings.model_validate({**BASE, 'runtime_id': 'daeri-1234567890'}))
    assert not any(operation == 'create' for operation, request in client.calls)
    assert client.calls[-1][0] == 'update_endpoint'


def test_runtime_confirms_aws_default_endpoint_without_modifying_it():
    client = Control(existing_endpoint=True)
    result = configure_runtime(client, RuntimeSettings.model_validate({**BASE, 'endpoint': 'DEFAULT'}))

    assert result['public_url'].endswith('/invocations?qualifier=DEFAULT')
    assert result['version'] == '2'
    assert not any(operation.endswith('_endpoint') for operation, _ in client.calls)


def test_runtime_reads_the_updated_version_even_if_an_unqualified_lookup_is_stale():
    client = Control(existing_endpoint=True)
    describe = client.get_agent_runtime

    def stale_unqualified(**request):
        result = describe(**request)
        if 'agentRuntimeVersion' not in request:
            return {**result, 'agentRuntimeVersion': '1', 'metadataConfiguration': {'requireMMDSV2': True}}
        return result

    client.get_agent_runtime = stale_unqualified
    result = configure_runtime(client, RuntimeSettings.model_validate({**BASE, 'runtime_id': 'daeri-1234567890'}))
    assert result['version'] == '2'
    assert ('get', {'agentRuntimeId': 'daeri-1234567890', 'agentRuntimeVersion': '2'}) in client.calls
    assert client.endpoint_version == '2'


def test_runtime_waits_for_both_default_endpoint_versions(monkeypatch):
    client = Control()
    observed = iter([('1', '1'), ('1', '2'), ('2', '2')])
    sleeps = []

    def endpoint(**request):
        assert request['endpointName'] == 'DEFAULT'
        live, target = next(observed)
        return {'status': 'READY', 'liveVersion': live, 'targetVersion': target}

    client.get_agent_runtime_endpoint = endpoint
    monkeypatch.setattr('daeri.infrastructure.runtime_deployment.time.sleep', sleeps.append)
    result = configure_runtime(client, RuntimeSettings.model_validate({**BASE, 'endpoint': 'DEFAULT'}))
    assert result['version'] == '2' and sleeps == [1, 1]
    assert not any(operation.endswith('_endpoint') for operation, _ in client.calls)


@pytest.mark.parametrize('metadata', [None, {'requireMMDSV2': False}])
def test_runtime_never_publishes_endpoint_without_confirmed_mmdsv2(metadata):
    client = Control()
    update = client.update_agent_runtime

    def not_applied(**request):
        result = update(**request)
        client.metadata = metadata
        return result
    client.update_agent_runtime = not_applied
    with pytest.raises(StoreError, match='RUNTIME_NOT_READY'):
        configure_runtime(client, RuntimeSettings.model_validate(BASE))
    assert not any(operation.endswith('_endpoint') for operation, _ in client.calls)


def test_failed_existing_runtime_is_not_updated_or_published():
    client = Control()
    client.get_agent_runtime = lambda **request: {'status': 'CREATE_FAILED'}
    with pytest.raises(StoreError, match='RUNTIME_NOT_READY'):
        configure_runtime(client, RuntimeSettings.model_validate(BASE))
    assert [operation for operation, request in client.calls] == ['create']


def test_runtime_wait_respects_shared_operation_deadline(monkeypatch):
    from daeri.infrastructure.runtime_deployment import _ready
    client = Control()
    client.get_agent_runtime = lambda **request: {'status': 'CREATING'}
    monkeypatch.setattr('daeri.infrastructure.runtime_deployment.time.monotonic', lambda: 200)
    monkeypatch.setattr('daeri.infrastructure.runtime_deployment.time.sleep',
                        lambda seconds: pytest.fail('An expired operation continued waiting.'))
    with pytest.raises(StoreError, match='RUNTIME_NOT_READY'):
        _ready(client, 'daeri-1234567890', deadline=100)


def test_configuration_check_does_not_require_aws_clients(monkeypatch):
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('Validation created an AWS client'))
    request = runtime_request(RuntimeSettings.model_validate(BASE))
    assert request['roleArn'] == BASE['role_arn']


@pytest.mark.parametrize('patch', [{'client_ids': []}, {'client_ids': ['same', 'same']},
                                   {'role_arn': 'wrong-role'}, {'image_uri': 'image token=private'},
                                   {'endpoint': 'bad/route'}])
def test_invalid_deployment_inputs_fail_before_aws(patch):
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({**BASE, **patch})


@pytest.mark.parametrize('network', [
    {}, {'networkMode': 'PRIVATE'},
    {'networkMode': 'PUBLIC', 'subnets': ['subnet-example']},
    {'networkMode': 'VPC'},
    {'networkMode': 'VPC', 'networkModeConfig': {'subnets': ['subnet-example']}},
    {'networkMode': 'VPC', 'networkModeConfig': {'subnets': [], 'securityGroups': ['sg-example']}},
    {'networkMode': 'VPC', 'networkModeConfig': {
        'subnets': ['subnet-12345678'], 'securityGroups': ['wrong-group']}},
    {'networkMode': 'VPC', 'networkModeConfig': {
        'subnets': ['wrong-subnet'], 'securityGroups': ['sg-12345678']}},
    {'networkMode': 'PUBLIC', 'networkModeConfig': {
        'subnets': ['subnet-example'], 'securityGroups': ['sg-example']}},
])
def test_invalid_native_network_configuration_is_rejected_before_deployment(network):
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({**BASE, 'network_configuration': network})


def test_native_vpc_network_configuration_is_preserved_in_the_runtime_request():
    network = {'networkMode': 'VPC', 'networkModeConfig': {
        'subnets': ['subnet-12345678'], 'securityGroups': ['sg-12345678']}}
    request = runtime_request(RuntimeSettings.model_validate({**BASE, 'network_configuration': network}))
    assert request['networkConfiguration'] == network


@pytest.mark.parametrize('target', ['runtime', 'connection'])
@pytest.mark.parametrize('field', ['subnets', 'securityGroups'])
@pytest.mark.parametrize('count', [0, 16, 17])
def test_runtime_and_connection_network_lists_obey_native_limits(target, field, count):
    network = {'networkMode': 'VPC', 'networkModeConfig': {
        'subnets': ['subnet-12345678'], 'securityGroups': ['sg-12345678']}}
    prefix = 'subnet' if field == 'subnets' else 'sg'
    network['networkModeConfig'][field] = [f'{prefix}-{index:08x}' for index in range(count)]
    model, data = (RuntimeSettings, BASE) if target == 'runtime' else (ProjectConnections, {
        'gateway_arn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example-abcdefghij',
        'allowed_connectors': []})
    if count == 16:
        assert model.model_validate({**data, 'network_configuration': network}).network_configuration == network
    else:
        with pytest.raises(ValidationError):
            model.model_validate({**data, 'network_configuration': network})


@pytest.mark.parametrize('target', ['runtime', 'connection'])
@pytest.mark.parametrize('legacy_s3_endpoint', [False, True])
def test_new_runtime_and_connection_deployments_reject_legacy_s3_endpoint_setting(target, legacy_s3_endpoint):
    network = {'networkMode': 'VPC', 'networkModeConfig': {
        'subnets': ['subnet-12345678'], 'securityGroups': ['sg-12345678'],
        'requireServiceS3Endpoint': legacy_s3_endpoint}}
    model, data = (RuntimeSettings, BASE) if target == 'runtime' else (ProjectConnections, {
        'gateway_arn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example-abcdefghij',
        'allowed_connectors': []})
    with pytest.raises(ValidationError):
        model.model_validate({**data, 'network_configuration': network})


@pytest.mark.parametrize('aws_config', ['', 'invalid-without-section'])
@pytest.mark.parametrize('valid_network', [False, True])
def test_network_validation_is_independent_of_aws_login_configuration(tmp_path, monkeypatch, aws_config, valid_network):
    from botocore.session import Session

    config = tmp_path / 'aws-config'
    config.write_text(aws_config)
    monkeypatch.setenv('AWS_CONFIG_FILE', str(config))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'no-credentials'))
    monkeypatch.setenv('AWS_PROFILE', 'profile-that-does-not-exist')
    monkeypatch.setattr(Session, 'create_client', lambda *a, **kw: pytest.fail('Validation created an AWS client'))
    network = {'networkMode': 'PUBLIC' if valid_network else 'PRIVATE'}
    if valid_network:
        request = runtime_request(RuntimeSettings.model_validate({**BASE, 'network_configuration': network}))
        assert request['networkConfiguration'] == network
    else:
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate({**BASE, 'network_configuration': network})


@pytest.mark.parametrize('apply', [False, True])
def test_runtime_command_rejects_invalid_network_before_creating_clients(tmp_path, monkeypatch, capsys, apply):
    import sys
    from scripts.configure_runtime import main
    from yaml import safe_dump

    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('Invalid input created an AWS client'))
    path = tmp_path / 'runtime.yaml'
    path.write_text(safe_dump({**BASE, 'network_configuration': {
        'networkMode': 'PUBLIC', 'private-value': 'should-stay-private'}}))
    monkeypatch.setattr(sys, 'argv', ['configure_runtime', '--config', str(path)] + (['--apply'] if apply else []))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert json.loads(output)['error']['code'] == 'INVALID_INPUT'
    assert 'private-value' not in output and 'should-stay-private' not in output
