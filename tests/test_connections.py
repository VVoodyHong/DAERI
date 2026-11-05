"""Project connections are validated and provisioned without touching live AWS."""

from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

from botocore.exceptions import ClientError
from botocore.loaders import create_loader
from botocore.model import ServiceModel
from botocore.validate import validate_parameters
import pytest

from daeri.core.errors import StoreError


PREFIX = '/daeri/connections/'
GATEWAY = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/project-abcdefghij'
HARNESS = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/Research-abcdefghij'
ROLE = 'arn:aws:iam::123456789012:role/connector'
SECRET = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:payments/database-ABCDEF'
RUNTIME_ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/Connector-abcdefghij'
RUNTIME = {'name': 'Database', 'kind': 'runtime', 'image_uri': '123456789012.dkr.ecr.us-east-1.amazonaws.com/database:v1',
           'command': ['database-mcp', '--transport', 'streamable-http'], 'environment': {'LOG_LEVEL': 'info'},
           'inputs': {'host': {'title': 'Host', 'env': 'DB_HOST'},
                      'password': {'title': 'Password reference', 'env': 'DB_PASSWORD', 'secret_ref': True},
                      'port': {'title': 'Port', 'type': 'integer', 'default': 5432, 'env': 'DB_PORT'}}}
MCP = {'name': 'Remote', 'kind': 'mcp', 'endpoint': 'https://{host}/mcp',
       'inputs': {'host': {'title': 'Host', 'default': 'tools.example.com'}}}
OPENAPI = {'name': 'API', 'kind': 'openapi', 'openapi_schema': {
    'openapi': '3.0.3', 'info': {'title': 'Example', 'version': '1'},
    'servers': [{'url': 'https://api.example.com'}], 'paths': {}}}
BINDING = {'gateway_arn': GATEWAY, 'allowed_connectors': ['database', 'remote', 'api'],
           'runtime_role_arn': ROLE, 'secret_arn_prefix': SECRET.rsplit('/', 1)[0] + '/'}
CREDENTIAL = {'credentialProviderType': 'API_KEY', 'credentialProvider': {'apiKeyCredentialProvider': {
    'providerArn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/apikeycredentialprovider/company',
    'credentialParameterName': 'x-api-key', 'credentialLocation': 'HEADER'}}}


def missing(operation):
    return ClientError({'Error': {'Code': 'ParameterNotFound' if 'Parameter' in operation else 'ResourceNotFoundException'}}, operation)


class Ssm:
    def __init__(self):
        self.values = {PREFIX + 'catalog/database': deepcopy(RUNTIME), PREFIX + 'catalog/remote': deepcopy(MCP),
                       PREFIX + 'catalog/api': deepcopy(OPENAPI), PREFIX + 'projects/payments': deepcopy(BINDING)}
        self.writes = []
        self.pages = 0

    def get_parameter(self, **request):
        if request['Name'] not in self.values:
            raise missing('GetParameter')
        return {'Parameter': {'Value': json.dumps(self.values[request['Name']])}}

    def put_parameter(self, **request):
        validate_parameters(request, ServiceModel(create_loader().load_service_model('ssm', 'service-2')).operation_model('PutParameter').input_shape)
        if request.get('Overwrite') is False and request['Name'] in self.values:
            raise ClientError({'Error': {'Code': 'ParameterAlreadyExists'}}, 'PutParameter')
        self.writes.append(request)
        self.values[request['Name']] = json.loads(request['Value'])
        return {'Version': len(self.writes)}

    def get_parameters_by_path(self, **request):
        self.pages += 1
        values = [{'Name': key, 'Value': json.dumps(value)} for key, value in sorted(self.values.items())
                  if key.startswith(request['Path'])]
        offset = int(request.get('NextToken', '0'))
        result = {'Parameters': values[offset:offset + 1]}
        if offset + 1 < len(values):
            result['NextToken'] = str(offset + 1)
        return result


class S3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.model = ServiceModel(create_loader().load_service_model('s3', 'service-2'))

    def put_object(self, **request):
        validate_parameters(request, self.model.operation_model('PutObject').input_shape)
        self.calls.append(('PutObject', request))
        if request['Key'] in self.objects:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        self.objects[request['Key']] = {'Body': request['Body'], 'ETag': '"etag"'}
        return {'ETag': '"etag"'}

    def delete_object(self, **request):
        validate_parameters(request, self.model.operation_model('DeleteObject').input_shape)
        self.calls.append(('DeleteObject', request))
        if self.objects[request['Key']]['ETag'] != request['IfMatch']:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'DeleteObject')
        del self.objects[request['Key']]


class Control:
    def __init__(self):
        self.model = ServiceModel(create_loader().load_service_model('bedrock-agentcore-control', 'service-2'))
        self.calls = []
        self.runtime = None
        self.endpoint = None
        self.target = None
        self.runtime_tags = {}
        self.other_targets = []
        self.other_runtimes = []
        self.harness_gateway = GATEWAY

    def record(self, operation, request):
        validate_parameters(request, self.model.operation_model(operation).input_shape)
        self.calls.append((operation, deepcopy(request)))

    def get_harness(self, **request):
        self.record('GetHarness', request)
        return {'harness': {'status': 'READY', 'harnessVersion': '2', 'tools': [
            {'type': 'agentcore_gateway', 'name': 'resources', 'config': {'agentCoreGateway': {'gatewayArn': self.harness_gateway}}}]}}

    def list_agent_runtimes(self, **request):
        self.record('ListAgentRuntimes', request)
        items = self.other_runtimes + ([self.runtime] if self.runtime else [])
        offset = int(request.get('nextToken', '0'))
        return {'agentRuntimes': items[offset:offset + 1], **({'nextToken': str(offset + 1)} if offset + 1 < len(items) else {})}

    def create_agent_runtime(self, **request):
        self.record('CreateAgentRuntime', request)
        self.runtime_tags = request.get('tags', {})
        self.runtime = {**deepcopy(request), 'agentRuntimeId': 'Connector-abcdefghij', 'agentRuntimeArn': RUNTIME_ARN,
                        'agentRuntimeVersion': '1', 'status': 'READY'}
        return deepcopy(self.runtime)

    def update_agent_runtime(self, **request):
        self.record('UpdateAgentRuntime', request)
        self.runtime.update(deepcopy(request))
        self.runtime['agentRuntimeVersion'] = str(int(self.runtime['agentRuntimeVersion']) + 1)
        return deepcopy(self.runtime)

    def get_agent_runtime(self, **request):
        self.record('GetAgentRuntime', request)
        if not self.runtime:
            raise missing('GetAgentRuntime')
        return deepcopy(self.runtime)

    def list_tags_for_resource(self, **request):
        self.record('ListTagsForResource', request)
        return {'tags': self.runtime_tags}

    def get_agent_runtime_endpoint(self, **request):
        self.record('GetAgentRuntimeEndpoint', request)
        if not self.endpoint:
            raise missing('GetAgentRuntimeEndpoint')
        return deepcopy(self.endpoint)

    def create_agent_runtime_endpoint(self, **request):
        self.record('CreateAgentRuntimeEndpoint', request)
        self.endpoint = {'status': 'READY', 'liveVersion': request['agentRuntimeVersion'], 'targetVersion': request['agentRuntimeVersion']}
        return self.endpoint

    def update_agent_runtime_endpoint(self, **request):
        self.record('UpdateAgentRuntimeEndpoint', request)
        self.endpoint = {'status': 'READY', 'liveVersion': request['agentRuntimeVersion'], 'targetVersion': request['agentRuntimeVersion']}
        return self.endpoint

    def list_gateway_targets(self, **request):
        self.record('ListGatewayTargets', request)
        items = self.other_targets + ([self.target] if self.target else [])
        offset = int(request.get('nextToken', '0'))
        return {'items': items[offset:offset + 1], **({'nextToken': str(offset + 1)} if offset + 1 < len(items) else {})}

    def create_gateway_target(self, **request):
        self.record('CreateGatewayTarget', request)
        self.target = {**deepcopy(request), 'targetId': '0123456789', 'status': 'READY'}
        return deepcopy(self.target)

    def update_gateway_target(self, **request):
        self.record('UpdateGatewayTarget', request)
        self.target.update(deepcopy(request))
        return deepcopy(self.target)

    def get_gateway_target(self, **request):
        self.record('GetGatewayTarget', request)
        return deepcopy(self.target)


def service():
    module = importlib.import_module('daeri.infrastructure.connections')
    control, ssm = Control(), Ssm()
    catalog = SimpleNamespace(get=lambda project_id: SimpleNamespace(id=project_id, harness_arn=HARNESS,
                              harness_version='2', harness_endpoint='RELEASE_V2'))
    locks = module.S3ConnectionLocks(S3(), 'daeri-bucket')
    return module.AwsConnections(control, ssm, catalog, locks=locks), control, ssm


def test_connector_schemas_and_describe_expose_fields_without_operator_secrets():
    backend, control, ssm = service()
    description = backend.describe('payments', 'database')
    assert description['fields']['password'] == {'title': 'Password reference', 'type': 'string', 'required': True, 'secret_ref': True}
    assert description['fields']['port']['default'] == 5432
    assert 'env' not in description['fields']['password']
    assert backend.describe('payments')['connectors'][0]['id'] == 'database'
    assert SECRET not in json.dumps(description) and not ssm.writes and not control.calls


def test_runtime_connection_uses_native_requests_secret_refs_and_reuses_project_resources():
    backend, control, ssm = service()
    inputs = {'host': 'db.internal', 'password': SECRET}
    first = backend.connect('payments', 'warehouse', 'database', inputs)
    assert first == {'id': 'warehouse', 'connector_id': 'database', 'name': 'Database', 'state': 'READY', 'resource_access': 'not_checked'}
    request = next(request for operation, request in control.calls if operation == 'CreateAgentRuntime')
    config = json.loads(request['environmentVariables']['DAERI_CONNECTOR_CONFIG'])
    assert config == {'command': RUNTIME['command'], 'environment': {'LOG_LEVEL': 'info', 'DB_HOST': 'db.internal', 'DB_PORT': '5432'},
                      'secrets': {'DB_PASSWORD': SECRET}}
    target = control.target
    assert target['targetConfiguration']['mcp']['mcpServer']['endpoint'].endswith('/invocations?qualifier=PROD')
    assert target['credentialProviderConfigurations'] == [{'credentialProviderType': 'GATEWAY_IAM_ROLE',
        'credentialProvider': {'iamCredentialProvider': {'service': 'bedrock-agentcore'}}}]
    backend.connect('payments', 'warehouse', 'database', {**inputs, 'host': 'new.internal'})
    assert len([1 for operation, _ in control.calls if operation == 'CreateAgentRuntime']) == 1
    assert len([1 for operation, _ in control.calls if operation == 'CreateGatewayTarget']) == 1
    assert backend.list('payments') == [first]
    assert 'password' not in json.dumps(first) and SECRET not in json.dumps(backend.list('payments'))
    assert all(request['harnessVersion'] == '2' for operation, request in control.calls if operation == 'GetHarness')


@pytest.mark.parametrize('connector,inputs', [('remote', {'host': 'tools.example.com'}), ('api', {})])
def test_remote_and_openapi_targets_use_trusted_credential_aliases(connector, inputs):
    backend, control, ssm = service()
    ssm.values[PREFIX + 'projects/payments']['credentials'] = {'team': [CREDENTIAL]}
    backend.connect('payments', 'service', connector, {**inputs, 'credential_id': 'team'})
    assert control.target['credentialProviderConfigurations'] == [CREDENTIAL]
    if connector == 'api':
        assert json.loads(control.target['targetConfiguration']['mcp']['openApiSchema']['inlinePayload']) == OPENAPI['openapi_schema']
    else:
        assert control.target['targetConfiguration']['mcp']['mcpServer']['endpoint'] == 'https://tools.example.com/mcp'
    assert control.runtime is None


@pytest.mark.parametrize('inputs', [
    {'host': 'db.internal', 'password': 'raw-password'}, {'host': 'db.internal', 'password': SECRET + '\n'},
    {'host': 'db.internal', 'password': SECRET.replace('payments/', 'other/')},
    {'host': 'db.internal', 'password': SECRET, 'image_uri': 'attacker/image'},
    {'host': 'db.internal', 'password': SECRET, 'port': True}, {'host': 'db.internal'},
])
def test_invalid_user_inputs_never_write_or_create_resources(inputs):
    backend, control, ssm = service()
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        backend.connect('payments', 'warehouse', 'database', inputs)
    assert not ssm.writes and not any(operation.startswith(('Create', 'Update')) for operation, _ in control.calls)


@pytest.mark.parametrize('inputs', [{}, {'credential_id': 'unknown'}, {'credential_id': 'team', 'token': 'raw'}])
def test_outbound_credentials_must_select_a_trusted_alias(inputs):
    backend, control, ssm = service()
    ssm.values[PREFIX + 'projects/payments']['credentials'] = {'team': [CREDENTIAL]}
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        backend.connect('payments', 'external', 'remote', inputs)
    assert not ssm.writes


def test_published_harness_gateway_mismatch_fails_before_any_write():
    backend, control, ssm = service()
    control.harness_gateway = GATEWAY + 'other'
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'external', 'remote', {})
    assert not ssm.writes and not any(operation.startswith(('Create', 'Update')) for operation, _ in control.calls)


def test_disallowed_connector_and_path_injection_are_rejected():
    backend, control, ssm = service()
    ssm.values[PREFIX + 'projects/payments']['allowed_connectors'] = ['database']
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        backend.connect('payments', 'external', 'remote', {})
    for project_id, connection_id in [('other/path', 'ok'), ('payments', '../other')]:
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            backend.connect(project_id, connection_id, 'database', {})
    assert not ssm.writes


def test_partial_failure_can_retry_without_creating_duplicate_runtime_and_sanitizes_errors():
    backend, control, ssm = service()
    create = control.create_gateway_target
    control.create_gateway_target = lambda **request: (_ for _ in ()).throw(RuntimeError('private-password'))
    with pytest.raises(StoreError) as error:
        backend.connect('payments', 'warehouse', 'database', {'host': 'db.internal', 'password': SECRET})
    assert 'private-password' not in str(error.value)
    assert backend.list('payments')[0]['state'] == 'FAILED'
    control.create_gateway_target = create
    assert backend.connect('payments', 'warehouse', 'database', {'host': 'db.internal', 'password': SECRET})['state'] == 'READY'
    assert len([1 for operation, _ in control.calls if operation == 'CreateAgentRuntime']) == 1


def test_owned_connection_cannot_change_connector_or_update_foreign_runtime_or_target():
    backend, control, ssm = service()
    inputs = {'host': 'db.internal', 'password': SECRET}
    backend.connect('payments', 'warehouse', 'database', inputs)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        backend.connect('payments', 'warehouse', 'remote', {})
    control.runtime_tags = {}
    before = len([1 for op, _ in control.calls if op.startswith('Update')])
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'warehouse', 'database', inputs)
    assert len([1 for op, _ in control.calls if op.startswith('Update')]) == before


def test_foreign_target_configuration_blocks_update():
    backend, control, ssm = service()
    backend.connect('payments', 'external', 'remote', {})
    control.target['targetConfiguration'] = {'mcp': {'mcpServer': {'endpoint': 'https://other.example.com/mcp'}}}
    before = len([1 for op, _ in control.calls if op == 'UpdateGatewayTarget'])
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'external', 'remote', {})
    assert len([1 for op, _ in control.calls if op == 'UpdateGatewayTarget']) == before


def test_pagination_and_project_scoped_listing_do_not_leak_registered_inputs():
    backend, control, ssm = service()
    control.other_targets = [{'name': 'other', 'targetId': '9999999999'}]
    control.other_runtimes = [{'agentRuntimeName': 'other', 'agentRuntimeId': 'Other-abcdefghij'}]
    backend.connect('payments', 'a', 'database', {'host': 'db.internal', 'password': SECRET})
    backend.connect('payments', 'a', 'database', {'host': 'db.internal', 'password': SECRET})
    backend.connect('payments', 'b', 'remote', {})
    ssm.values[PREFIX + 'registered/other/private'] = {'id': 'private', 'password': 'private'}
    listed = backend.list('payments')
    assert {item['id'] for item in listed} == {'a', 'b'} and SECRET not in json.dumps(listed)
    assert ssm.pages >= 2
    assert any(request.get('nextToken') for op, request in control.calls if op == 'ListGatewayTargets')


def test_config_validation_is_inert_and_rejects_invalid_native_or_raw_secret_fields(monkeypatch):
    from pydantic import ValidationError
    from botocore.session import Session
    monkeypatch.setattr(Session, 'create_client', lambda *a, **kw: pytest.fail('Configuration created an AWS client'))
    models = importlib.import_module('daeri.config.connections')
    assert models.ConnectionSettings(prefix='/daeri/connections').prefix == PREFIX
    assert models.ProjectConnections.model_validate(BINDING).network_configuration == {'networkMode': 'PUBLIC'}
    for config in [{**BINDING, 'network_configuration': {'networkMode': 'PRIVATE'}},
                   {**BINDING, 'credentials': {'bad': [{'credentialProviderType': 'UNKNOWN'}]}},
                   {**BINDING, 'credentials': {'bad': [{'credentialProviderType': 'API_KEY', 'credentialProvider': {'apiKeyCredentialProvider': {'providerArn': 'raw-token'}}}]}}]:
        with pytest.raises(ValidationError):
            models.ProjectConnections.model_validate(config)
    for config in [{**RUNTIME, 'command': 'shell command'}, {**MCP, 'endpoint': 'http://unsafe/mcp'},
                   {**RUNTIME, 'inputs': {'password': {'title': 'Password', 'env': 'PASSWORD'}}}]:
        with pytest.raises(ValidationError):
            models.ConnectorTemplate.model_validate(config)


@pytest.mark.parametrize('patch', [{'gateway_arn': GATEWAY}, {'gateway_arn': GATEWAY + 'other', 'secret_arn_prefix': SECRET.rsplit('/', 1)[0] + '/'}])
def test_project_bindings_cannot_share_gateway_or_secret_scope(patch):
    backend, control, ssm = service()
    ssm.values[PREFIX + 'projects/other'] = {**BINDING, 'secret_arn_prefix': None, **patch}
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'external', 'remote', {})
    assert not ssm.writes


def test_target_failed_discovery_is_saved_as_failed_without_connected_claim():
    backend, control, ssm = service()
    create = control.create_gateway_target
    def failed(**request):
        result = create(**request)
        control.target['status'] = 'FAILED'
        return result
    control.create_gateway_target = failed
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'external', 'remote', {})
    assert backend.list('payments')[0]['state'] == 'FAILED'


def test_existing_provisioning_lock_prevents_concurrent_writes_and_requires_inspection():
    backend, control, ssm = service()
    key = 'daeri/connection-locks/' + backend._owner('payments', 'external') + '.json'
    backend.locks.client.objects[key] = {'Body': b'other', 'ETag': '"other"'}
    with pytest.raises(StoreError, match='CONNECTION_BUSY'):
        backend.connect('payments', 'external', 'remote', {})
    assert not ssm.writes and control.target is None
    del backend.locks.client.objects[key]
    backend.connect('payments', 'external', 'remote', {})
    assert not backend.locks.client.objects


def test_provisioning_failure_releases_lock_for_same_id_retry():
    backend, control, ssm = service()
    control.create_gateway_target = lambda **request: (_ for _ in ()).throw(RuntimeError('sensitive'))
    with pytest.raises(StoreError):
        backend.connect('payments', 'external', 'remote', {})
    assert not backend.locks.client.objects


@pytest.mark.parametrize('patch', [{'environment': {'DAERI_CONNECTOR_CONFIG': 'private'}},
                                  {'inputs': {'host': {'title': 'Host', 'env': 'DAERI_CONNECTOR_CONFIG'}}}])
def test_runtime_template_cannot_overwrite_bootstrap_control_environment(patch):
    from daeri.config.connections import ConnectorTemplate
    with pytest.raises(ValueError):
        ConnectorTemplate.model_validate({**RUNTIME, **patch})


def test_owner_can_supply_a_full_https_mcp_endpoint_and_api_server_url():
    backend, control, ssm = service()
    ssm.values[PREFIX + 'catalog/remote'] = {'name': 'Remote', 'kind': 'mcp', 'endpoint': '{endpoint}',
        'inputs': {'endpoint': {'title': 'MCP endpoint'}}}
    backend.connect('payments', 'external', 'remote', {'endpoint': 'https://tools.example.com/custom/mcp'})
    assert control.target['targetConfiguration']['mcp']['mcpServer']['endpoint'] == 'https://tools.example.com/custom/mcp'
    backend2, control2, ssm2 = service()
    ssm2.values[PREFIX + 'catalog/api']['inputs'] = {'endpoint': {'title': 'API URL'}}
    ssm2.values[PREFIX + 'catalog/api']['openapi_schema']['servers'][0]['url'] = '{endpoint}'
    ssm2.values[PREFIX + 'catalog/api']['openapi_schema']['paths'] = {'/items/{itemId}': {'get': {'operationId': 'getItem'}}}
    backend2.connect('payments', 'api', 'api', {'endpoint': 'https://project.example.com/v1'})
    schema = json.loads(control2.target['targetConfiguration']['mcp']['openApiSchema']['inlinePayload'])
    assert schema['servers'][0]['url'] == 'https://project.example.com/v1'
    assert '/items/{itemId}' in schema['paths']


@pytest.mark.parametrize('endpoint', ['http://tools.example.com/mcp', 'https://user:password@tools.example.com/mcp', 'https://tools.example.com/mcp#private'])
def test_invalid_rendered_endpoint_is_rejected_before_registration(endpoint):
    backend, control, ssm = service()
    ssm.values[PREFIX + 'catalog/remote'] = {'name': 'Remote', 'kind': 'mcp', 'endpoint': '{endpoint}',
        'inputs': {'endpoint': {'title': 'MCP endpoint'}}}
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        backend.connect('payments', 'external', 'remote', {'endpoint': endpoint})
    assert not ssm.writes and control.target is None


def test_runtime_authorizer_drift_is_rejected_without_updating_resources():
    backend, control, ssm = service()
    inputs = {'host': 'db.internal', 'password': SECRET}
    backend.connect('payments', 'warehouse', 'database', inputs)
    control.runtime['authorizerConfiguration'] = {'customJWTAuthorizer': {'discoveryUrl': 'https://login.example.com/.well-known/openid-configuration'}}
    before = len([1 for op, _ in control.calls if op.startswith('Update')])
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'warehouse', 'database', inputs)
    assert len([1 for op, _ in control.calls if op.startswith('Update')]) == before


@pytest.mark.parametrize('provider', [
    {'credentialProviderType': 'OAUTH', 'credentialProvider': {'oauthCredentialProvider': {
        'providerArn': 'arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/company', 'scopes': [], 'grantType': 'AUTHORIZATION_CODE'}}},
    {'credentialProviderType': 'JWT_PASSTHROUGH'}, {'credentialProviderType': 'CALLER_IAM_CREDENTIALS'},
])
def test_new_shared_connections_require_project_service_credentials(provider):
    from daeri.config.connections import ProjectConnections
    with pytest.raises(ValueError):
        ProjectConnections.model_validate({**BINDING, 'credentials': {'personal': [provider]}})


def test_native_runtime_environment_maximum_is_checked_before_any_write():
    backend, control, ssm = service()
    ssm.values[PREFIX + 'catalog/database']['environment']['LARGE_CONFIG'] = 'x' * 4900
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        backend.connect('payments', 'warehouse', 'database', {'host': 'db.internal', 'password': SECRET})
    assert not ssm.writes and control.runtime is None


def test_s3_lock_is_conditionally_created_and_only_its_etag_can_release_it():
    from daeri.infrastructure.connections import S3ConnectionLocks
    client = S3()
    locks = S3ConnectionLocks(client, 'daeri-bucket')
    with pytest.raises(StoreError, match='CONNECTION_BUSY'):
        with locks.hold('a' * 64):
            create = client.calls[-1][1]
            assert create['IfNoneMatch'] == '*' and create['Key'] == 'daeri/connection-locks/' + 'a' * 64 + '.json'
            client.objects[create['Key']]['ETag'] = '"replacement"'
    # A replacement lock remains and cannot be removed by the original holder.
    assert client.objects[create['Key']]['ETag'] == '"replacement"'


def test_connect_requires_cross_process_locks_without_writing():
    backend, control, ssm = service()
    backend.locks = None
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'external', 'remote', {})
    assert not ssm.writes and control.target is None


def test_form_revision_changes_when_hidden_operator_authority_changes():
    backend, control, ssm = service()
    before = backend.describe('payments', 'database')
    ssm.values[PREFIX + 'catalog/database']['image_uri'] += '-updated'
    assert backend.describe('payments', 'database')['revision'] != before['revision']
    current = backend.describe('payments', 'database')
    ssm.values[PREFIX + 'projects/payments']['runtime_role_arn'] += '-updated'
    assert backend.describe('payments', 'database')['revision'] != current['revision']


def test_expected_form_revision_rejects_config_reload_race_before_writes():
    backend, control, ssm = service()
    revision = backend.describe('payments', 'database')['revision']
    ssm.values[PREFIX + 'catalog/database']['image_uri'] += '-changed'
    with pytest.raises(StoreError, match='CONNECTION_CHANGED'):
        backend.connect('payments', 'warehouse', 'database', {'host': 'db.internal', 'password': SECRET}, expected_revision=revision)
    assert not ssm.writes and not backend.locks.client.objects and control.runtime is None


@pytest.mark.parametrize('endpoint', ['https://tools.example.com/mcp?token=raw',
                                     'https://tools.example.com/mcp?api_key=raw',
                                     'https://tools.example.com/mcp?password=raw'])
def test_endpoint_inputs_cannot_embed_raw_query_credentials(endpoint):
    backend, control, ssm = service()
    ssm.values[PREFIX + 'catalog/remote'] = {'name': 'Remote', 'kind': 'mcp', 'endpoint': '{endpoint}',
        'inputs': {'endpoint': {'title': 'Endpoint'}}}
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        backend.connect('payments', 'external', 'remote', {'endpoint': endpoint})
    assert not ssm.writes


def test_empty_or_foreign_registration_is_not_silently_overwritten():
    backend, control, ssm = service()
    ssm.values[PREFIX + 'registered/payments/external'] = {}
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'external', 'remote', {})
    assert not ssm.writes and control.target is None


def test_sdk_errors_with_malformed_response_are_sanitized():
    backend, control, ssm = service()
    class BrokenError(Exception):
        response = None
    get_parameter = ssm.get_parameter
    def broken_registration(**request):
        if 'registered/' in request['Name']:
            raise BrokenError('private-token')
        return get_parameter(**request)
    ssm.get_parameter = broken_registration
    with pytest.raises(StoreError) as error:
        backend.connect('payments', 'external', 'remote', {})
    assert 'private-token' not in str(error.value)


def test_lock_identifies_project_and_connection_before_registration_exists():
    backend, control, ssm = service()
    backend.connect('payments', 'external', 'remote', {})
    create = backend.locks.client.calls[0][1]
    assert create['Metadata'] == {'project-id': 'payments', 'connection-id': 'external'}


def test_explicit_runtime_reconfiguration_refreshes_target_discovery_at_same_endpoint():
    backend, control, ssm = service()
    inputs = {'host': 'db.internal', 'password': SECRET}
    backend.connect('payments', 'warehouse', 'database', inputs)
    endpoint = control.target['targetConfiguration']['mcp']['mcpServer']['endpoint']
    ssm.values[PREFIX + 'catalog/database']['image_uri'] += '-updated'
    backend.connect('payments', 'warehouse', 'database', inputs)
    updates = [request for operation, request in control.calls if operation == 'UpdateGatewayTarget']
    assert len(updates) == 1
    assert updates[0]['targetConfiguration']['mcp']['mcpServer']['endpoint'] == endpoint
    assert control.calls[-1][0] == 'GetGatewayTarget'


def test_connector_runtime_enables_mmdsv2_before_publishing_its_endpoint():
    backend, control, _ = service()
    create_endpoint = control.create_agent_runtime_endpoint

    def publish(**request):
        assert control.runtime.get('metadataConfiguration') == {'requireMMDSV2': True}
        return create_endpoint(**request)
    control.create_agent_runtime_endpoint = publish
    result = backend.connect('payments', 'warehouse', 'database', {'host': 'db.internal', 'password': SECRET})
    assert result['state'] == 'READY'
    updates = [request for operation, request in control.calls if operation == 'UpdateAgentRuntime']
    assert updates[0]['metadataConfiguration'] == {'requireMMDSV2': True}

    control.runtime['metadataConfiguration'] = {'requireMMDSV2': False}
    backend.connect('payments', 'warehouse', 'database', {'host': 'db.internal', 'password': SECRET})
    assert control.runtime['metadataConfiguration'] == {'requireMMDSV2': True}


@pytest.mark.parametrize('metadata', [None, {'requireMMDSV2': False}])
def test_connector_never_publishes_endpoint_without_confirmed_mmdsv2(metadata):
    backend, control, _ = service()
    update = control.update_agent_runtime

    def not_applied(**request):
        result = update(**request)
        control.runtime['metadataConfiguration'] = metadata
        return result
    control.update_agent_runtime = not_applied
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        backend.connect('payments', 'warehouse', 'database', {'host': 'db.internal', 'password': SECRET})
    assert control.endpoint is None and control.target is None
