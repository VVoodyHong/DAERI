"""Provision reusable project connections with native AgentCore and SSM APIs."""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import re
import time
from uuid import uuid4

from daeri.config.connections import (ConnectionSettings, ConnectorTemplate, ProjectConnections,
                                      _SECRET_ARN, render_url, validate_project_bindings)
from daeri.config.validation import control_model, validate_native
from daeri.core.endpoints import runtime_endpoint
from daeri.core.errors import StoreError
from daeri.core.projects import valid_harness_release
from daeri.core.serialization import validate_storable_json
from daeri.infrastructure.cloud_s3 import normalize_storage_prefix, validate_storage_bucket
from daeri.infrastructure.project_catalog import _PROJECT_ID
from daeri.infrastructure.runtime_deployment import _ready


def _identifier(value):
    if not isinstance(value, str) or not _PROJECT_ID.fullmatch(value):
        raise StoreError('INVALID_INPUT', 'Supply a connection identifier using letters, digits, underscores or hyphens.')
    return value


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _revision(template, binding):
    return _digest([template.model_dump(mode='json'), binding.model_dump(mode='json')])


def _code(error):
    response = getattr(error, 'response', None)
    details = response.get('Error') if isinstance(response, dict) else None
    return details.get('Code') if isinstance(details, dict) else None


def _summary(record):
    return {**{key: record[key] for key in ('id', 'connector_id', 'name', 'state')}, 'resource_access': 'not_checked'}


class S3ConnectionLocks:
    """A conditional object serializes updates; interrupted workers leave an inspectable lock."""
    def __init__(self, client, bucket, prefix='daeri/'):
        self.client = client
        self.bucket = validate_storage_bucket(bucket)
        self.prefix = normalize_storage_prefix(prefix) + 'connection-locks/'

    @contextmanager
    def hold(self, owner, *, project_id=None, connection_id=None):
        if not isinstance(owner, str) or not re.fullmatch('[0-9a-f]{64}', owner):
            raise StoreError('INVALID_INPUT', 'Supply a valid connection lock identifier.')
        key = self.prefix + owner + '.json'
        metadata = {} if project_id is None and connection_id is None else {
            'project-id': _identifier(project_id), 'connection-id': _identifier(connection_id)}
        try:
            result = self.client.put_object(Bucket=self.bucket, Key=key, Body=uuid4().hex.encode(),
                                            ContentType='application/octet-stream', IfNoneMatch='*', Metadata=metadata)
            etag = result['ETag']
            if not isinstance(etag, str) or not etag:
                raise ValueError('Missing lock ETag')
        except Exception as error:
            if _code(error) in {'PreconditionFailed', 'ConditionalRequestConflict', '412', '409'}:
                raise StoreError('CONNECTION_BUSY', 'Connection configuration is running or interrupted; inspect the saved connection and exact provisioning lock before retrying.') from None
            raise StoreError('CONNECTION_UNAVAILABLE', 'The connection provisioning lock could not be acquired; inspect it before retrying.') from None
        try:
            yield
        finally:
            try:
                self.client.delete_object(Bucket=self.bucket, Key=key, IfMatch=etag)
            except Exception:
                raise StoreError('CONNECTION_BUSY', 'The provisioning lock could not be released; inspect the connection and exact lock before retrying.') from None


class AwsConnections:
    def __init__(self, control, ssm, catalog, *, prefix='/daeri/connections/', locks=None):
        self.control, self.ssm, self.catalog, self.locks = control, ssm, catalog, locks
        self.prefix = ConnectionSettings(prefix=prefix).prefix

    def _read(self, suffix, *, optional=False):
        try:
            result = self.ssm.get_parameter(Name=self.prefix + suffix, WithDecryption=False)
            value = json.loads(result['Parameter']['Value'])
            validate_storable_json(value)
            if not isinstance(value, dict):
                raise ValueError('Invalid metadata')
            return value
        except Exception as error:
            if optional and _code(error) == 'ParameterNotFound':
                return None
            raise StoreError('CONNECTION_UNAVAILABLE', 'The connector configuration could not be read; check its registration.') from None

    def _pages(self, path):
        request = {'Path': self.prefix + path, 'Recursive': False, 'WithDecryption': False, 'MaxResults': 10}
        seen = set()
        while True:
            result = self.ssm.get_parameters_by_path(**request)
            for item in result.get('Parameters', []):
                name = item['Name']
                if not name.startswith(request['Path']) or '/' in name[len(request['Path']):]:
                    raise ValueError('Invalid parameter path')
                value = json.loads(item['Value'])
                validate_storable_json(value)
                yield _identifier(name[len(request['Path']):]), value
            token = result.get('NextToken')
            if not token:
                return
            if token in seen:
                raise ValueError('Repeated page token')
            seen.add(token)
            request['NextToken'] = token

    def _binding(self, project_id):
        _identifier(project_id)
        try:
            return ProjectConnections.model_validate(self._read('projects/' + project_id))
        except StoreError:
            raise
        except Exception:
            raise StoreError('CONNECTION_UNAVAILABLE', 'The project connector binding is invalid; ask its operator to check it.') from None

    def _template(self, binding, connector_id):
        _identifier(connector_id)
        if connector_id not in binding.allowed_connectors:
            raise StoreError('ACCESS_DENIED', 'This connector is not available to the selected project.')
        try:
            return ConnectorTemplate.model_validate(self._read('catalog/' + connector_id))
        except StoreError:
            raise
        except Exception:
            raise StoreError('CONNECTION_UNAVAILABLE', 'The selected connector template is invalid; ask its operator to check it.') from None

    def describe(self, project_id, connector_id=None):
        binding = self._binding(project_id)
        if connector_id is None:
            return {'connectors': [{'id': identifier, 'name': template.name, 'description': template.description}
                    for identifier in binding.allowed_connectors
                    for template in [self._template(binding, identifier)]], 'credentials': list(binding.credentials)}
        template = self._template(binding, connector_id)
        fields = {}
        for name, field in template.inputs.items():
            fields[name] = {'title': field.title, 'type': field.type, 'required': field.required, 'secret_ref': field.secret_ref}
            if 'default' in field.model_fields_set:
                fields[name]['default'] = field.default
        return {'name': template.name, 'fields': fields, 'credentials': [] if template.kind == 'runtime' else list(binding.credentials),
                'revision': _revision(template, binding)}

    def list(self, project_id):
        self._binding(project_id)
        try:
            result = []
            for identifier, record in self._pages('registered/' + project_id + '/'):
                self._check_record(record, project_id, identifier)
                result.append(_summary(record))
            return result
        except StoreError:
            raise
        except Exception:
            raise StoreError('CONNECTION_UNAVAILABLE', 'The project connection list could not be read.') from None

    def _check_record(self, record, project_id, connection_id):
        if (not isinstance(record, dict) or record.get('id') != connection_id or record.get('project_id') != project_id
                or record.get('owner') != self._owner(project_id, connection_id)
                or record.get('kind') not in {'runtime', 'mcp', 'openapi'}
                or record.get('state') not in {'PROVISIONING', 'READY', 'FAILED'}
                or not isinstance(record.get('name'), str)):
            raise StoreError('CONNECTION_NOT_READY', 'The saved connection ownership is invalid; ask its operator to inspect it.')
        _identifier(record.get('connector_id'))

    def _owner(self, project_id, connection_id):
        return _digest([self.prefix, project_id, connection_id])

    def _save(self, record):
        value = json.dumps(record, separators=(',', ':'))
        if len(value.encode()) > 8192:
            raise StoreError('INVALID_INPUT', 'Connection settings exceed the supported registration size.')
        self.ssm.put_parameter(Name=self.prefix + 'registered/' + record['project_id'] + '/' + record['id'],
                               Value=value, Type='String', Overwrite=True, Tier='Intelligent-Tiering')

    @staticmethod
    def _inputs(template, binding, inputs):
        try:
            validate_storable_json(inputs)
            if not isinstance(inputs, dict) or set(inputs) - set(template.inputs) - {'credential_id'}:
                raise ValueError('Unsupported fields')
            values = {}
            for name, field in template.inputs.items():
                if name in inputs:
                    value = inputs[name]
                elif 'default' in field.model_fields_set:
                    value = field.default
                elif field.required:
                    raise ValueError('Required field')
                else:
                    continue
                if type(value) is not {'string': str, 'integer': int, 'boolean': bool}[field.type]:
                    raise ValueError('Invalid type')
                if isinstance(value, str) and (not value or len(value) > 4096 or '\x00' in value):
                    raise ValueError('Invalid field')
                if field.secret_ref and (not _SECRET_ARN.fullmatch(value) or not binding.secret_arn_prefix
                                         or not value.startswith(binding.secret_arn_prefix)):
                    raise ValueError('Invalid secret scope')
                values[name] = value
            alias = inputs.get('credential_id')
            if template.kind == 'runtime':
                if alias is not None:
                    raise ValueError('Runtime IAM credentials are fixed')
            elif binding.credentials:
                if not isinstance(alias, str) or alias not in binding.credentials:
                    raise ValueError('Select a credential alias')
                values['credential_id'] = alias
            elif alias is not None:
                raise ValueError('Unknown credential alias')
            return values
        except Exception:
            raise StoreError('INVALID_INPUT', 'Supply the declared connection fields and project credential references.') from None

    def _preflight(self, project_id, binding):
        try:
            projects = {identifier: ProjectConnections.model_validate(value) for identifier, value in self._pages('projects/')}
            projects[project_id] = binding
            validate_project_bindings(projects)
            project = self.catalog.get(project_id)
            if not valid_harness_release(project.harness_endpoint, project.harness_version):
                raise ValueError('Unpinned release')
            harness = self.control.get_harness(harnessId=project.harness_arn.rsplit('/', 1)[1],
                                                harnessVersion=project.harness_version)['harness']
            attached = any(tool.get('config', {}).get('agentCoreGateway', {}).get('gatewayArn') == binding.gateway_arn
                           for tool in harness.get('tools', []))
            if (harness.get('status') != 'READY' or harness.get('harnessVersion') != project.harness_version or not attached):
                raise ValueError('Gateway not connected')
        except StoreError:
            raise
        except Exception:
            raise StoreError('CONNECTION_NOT_READY', 'Verify the project dedicated Gateway, secret scope and published harness release before retrying.') from None

    def _resources(self, operation, field, **request):
        request['maxResults'] = 100
        seen = set()
        while True:
            result = getattr(self.control, operation)(**request)
            yield from result.get(field, [])
            token = result.get('nextToken')
            if not token:
                return
            if token in seen:
                raise ValueError('Repeated page token')
            seen.add(token)
            request['nextToken'] = token

    @staticmethod
    def _runtime_request(template, binding, values):
        environment, secrets = dict(template.environment), {}
        for key, value in values.items():
            field = template.inputs[key]
            if field.secret_ref:
                secrets[field.env] = value
            else:
                environment[field.env] = str(value).lower() if isinstance(value, bool) else str(value)
        request = {'agentRuntimeArtifact': {'containerConfiguration': {'containerUri': template.image_uri}},
                   'roleArn': binding.runtime_role_arn, 'networkConfiguration': binding.network_configuration,
                   'protocolConfiguration': {'serverProtocol': 'MCP'}, 'environmentVariables': {'DAERI_CONNECTOR_CONFIG':
                   json.dumps({'command': template.command, 'environment': environment, 'secrets': secrets}, separators=(',', ':'))}}
        return request

    @staticmethod
    def _target_config(template, binding, values, runtime_url='https://example.com/mcp'):
        if template.kind == 'runtime':
            return ({'mcp': {'mcpServer': {'endpoint': runtime_url}}},
                    [{'credentialProviderType': 'GATEWAY_IAM_ROLE', 'credentialProvider': {'iamCredentialProvider': {'service': 'bedrock-agentcore'}}}])
        providers = binding.credentials.get(values.get('credential_id'), [])
        if template.kind == 'mcp':
            return {'mcp': {'mcpServer': {'endpoint': render_url(template.endpoint, values)}}}, providers
        schema = deepcopy(template.openapi_schema)
        for server in schema['servers']:
            server['url'] = render_url(server['url'], values)
        return {'mcp': {'openApiSchema': {'inlinePayload': json.dumps(schema, separators=(',', ':'))}}}, providers

    def _validate_requests(self, template, binding, values, owner):
        try:
            if template.kind == 'runtime':
                if not binding.runtime_role_arn:
                    raise StoreError('CONNECTION_NOT_READY', 'The project needs a connector Runtime execution role.')
                request = self._runtime_request(template, binding, values)
                validate_native({'agentRuntimeName': 'Connection_' + owner[:32], **request},
                                control_model().operation_model('CreateAgentRuntime').input_shape)
            config, providers = self._target_config(template, binding, values)
            request = {'gatewayIdentifier': binding.gateway_arn.rsplit('/', 1)[1], 'name': 'connection-' + owner[:32],
                       'description': 'DAERI connection ' + owner, 'targetConfiguration': config}
            if providers:
                request['credentialProviderConfigurations'] = providers
            validate_native(request, control_model().operation_model('CreateGatewayTarget').input_shape)
        except StoreError:
            raise
        except Exception:
            raise StoreError('INVALID_INPUT', 'The supplied connection fields do not form a valid native connector request.') from None

    def _runtime(self, template, binding, values, record):
        deadline = time.monotonic() + 180
        if not binding.runtime_role_arn:
            raise StoreError('CONNECTION_NOT_READY', 'The project needs a connector Runtime execution role.')
        runtime_id = record.get('runtime_id')
        name = 'Connection_' + record['owner'][:32]
        if not runtime_id:
            matches = [item for item in self._resources('list_agent_runtimes', 'agentRuntimes') if item.get('agentRuntimeName') == name]
            if len(matches) > 1:
                raise StoreError('CONNECTION_NOT_READY', 'Multiple connector Runtimes need operator inspection.')
            if matches:
                runtime_id = matches[0]['agentRuntimeId']
        tags = {'daeri:project': record['project_id'], 'daeri:connections': self.prefix, 'daeri:connection': record['id']}
        request = self._runtime_request(template, binding, values)
        if runtime_id:
            current = _ready(self.control, runtime_id, deadline=deadline)
            actual_tags = self.control.list_tags_for_resource(resourceArn=current['agentRuntimeArn']).get('tags', {})
            if (current.get('agentRuntimeName') != name or current.get('authorizerConfiguration')
                    or current.get('requestHeaderConfiguration')
                    or any(actual_tags.get(key) != value for key, value in tags.items())):
                raise StoreError('CONNECTION_NOT_READY', 'The connector Runtime ownership does not match its project registration.')
        else:
            created = self.control.create_agent_runtime(agentRuntimeName=name, tags=tags, clientToken=record['owner'], **request)
            runtime_id = created['agentRuntimeId']
        record['runtime_id'] = runtime_id
        self._save(record)
        current = _ready(self.control, runtime_id, deadline=deadline)
        # CreateAgentRuntime does not accept metadataConfiguration; enable MMDSv2 through Update.
        if (any(current.get(key) != value for key, value in request.items())
                or (current.get('metadataConfiguration') or {}).get('requireMMDSV2') is not True):
            self.control.update_agent_runtime(agentRuntimeId=runtime_id, metadataConfiguration={'requireMMDSV2': True}, **request)
            current = _ready(self.control, runtime_id, deadline=deadline)
        if (any(current.get(key) != value for key, value in request.items())
                or (current.get('metadataConfiguration') or {}).get('requireMMDSV2') is not True):
            raise StoreError('CONNECTION_NOT_READY', 'The configured Runtime version could not be confirmed.')
        try:
            endpoint = self.control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName='PROD')
        except Exception as error:
            if _code(error) != 'ResourceNotFoundException':
                raise
            self.control.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name='PROD', agentRuntimeVersion=current['agentRuntimeVersion'])
        else:
            if endpoint.get('liveVersion') != current['agentRuntimeVersion'] or endpoint.get('targetVersion') != current['agentRuntimeVersion']:
                self.control.update_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName='PROD', agentRuntimeVersion=current['agentRuntimeVersion'])
        endpoint = _ready(self.control, runtime_id, endpoint='PROD', deadline=deadline)
        if endpoint.get('liveVersion') != current['agentRuntimeVersion'] or endpoint.get('targetVersion') != current['agentRuntimeVersion']:
            raise StoreError('CONNECTION_NOT_READY', 'The connector endpoint version could not be confirmed.')
        return runtime_endpoint(current['agentRuntimeArn'], 'PROD')

    def _target(self, template, binding, values, record):
        endpoint = self._runtime(template, binding, values, record) if template.kind == 'runtime' else None
        config, providers = self._target_config(template, binding, values, endpoint)
        name, marker = 'connection-' + record['owner'][:32], 'DAERI connection ' + record['owner']
        gateway = binding.gateway_arn.rsplit('/', 1)[1]
        desired = _digest([config, providers])
        matches = [item for item in self._resources('list_gateway_targets', 'items', gatewayIdentifier=gateway) if item.get('name') == name]
        if len(matches) > 1:
            raise StoreError('CONNECTION_NOT_READY', 'Multiple Gateway targets need operator inspection.')
        current = None
        if record.get('target_id'):
            current = self.control.get_gateway_target(gatewayIdentifier=gateway, targetId=record['target_id'])
        elif matches:
            current = self.control.get_gateway_target(gatewayIdentifier=gateway, targetId=matches[0]['targetId'])
        if current:
            fingerprint = _digest([current.get('targetConfiguration'), current.get('credentialProviderConfigurations', [])])
            allowed = {record.get('target_fingerprint'), record.get('pending_target_fingerprint'), desired}
            if (current.get('name') != name or current.get('description') != marker or fingerprint not in allowed
                    or (matches and matches[0]['targetId'] != current['targetId'])):
                raise StoreError('CONNECTION_NOT_READY', 'The Gateway target ownership or configuration has changed; inspect it before retrying.')
        record['pending_target_fingerprint'] = desired
        self._save(record)
        request = {'gatewayIdentifier': gateway, 'name': name, 'description': marker, 'targetConfiguration': config}
        if providers:
            request['credentialProviderConfigurations'] = providers
        if current:
            target_id = current['targetId']
            # Re-discover tools even when a mutable Runtime endpoint URL has not changed.
            self.control.update_gateway_target(targetId=target_id, **request)
        else:
            created = self.control.create_gateway_target(clientToken=record['owner'], **request)
            target_id = created['targetId']
        record['target_id'] = target_id
        self._save(record)
        for attempt in range(30):
            target = self.control.get_gateway_target(gatewayIdentifier=gateway, targetId=target_id)
            if target.get('status') == 'READY':
                if _digest([target.get('targetConfiguration'), target.get('credentialProviderConfigurations', [])]) != desired:
                    break
                record['target_fingerprint'] = desired
                record.pop('pending_target_fingerprint', None)
                return
            if target.get('status') not in {'CREATING', 'UPDATING', 'SYNCHRONIZING'}:
                break
            if attempt < 29:
                time.sleep(1)
        raise StoreError('CONNECTION_NOT_READY', 'The Gateway target discovery is not ready; inspect the saved connection before retrying.')

    def connect(self, project_id, connection_id, connector_id, inputs, *, expected_revision=None):
        _identifier(project_id)
        _identifier(connection_id)
        if self.locks is None:
            raise StoreError('CONNECTION_NOT_READY', 'Configure the project storage provisioning locks before registering connections.')
        binding = self._binding(project_id)
        template = self._template(binding, connector_id)
        if expected_revision is not None and expected_revision != _revision(template, binding):
            raise StoreError('CONNECTION_CHANGED', 'The connector settings changed; reopen its configuration form before retrying.')
        values = self._inputs(template, binding, inputs)
        owner = self._owner(project_id, connection_id)
        self._validate_requests(template, binding, values, owner)
        self._preflight(project_id, binding)
        with self.locks.hold(owner, project_id=project_id, connection_id=connection_id):
            record = self._read('registered/' + project_id + '/' + connection_id, optional=True)
            if record is not None:
                self._check_record(record, project_id, connection_id)
                if record['connector_id'] != connector_id or record['kind'] != template.kind:
                    raise StoreError('INVALID_INPUT', 'Use a new connection ID to choose a different connector.')
                if record.get('gateway_arn') != binding.gateway_arn:
                    raise StoreError('CONNECTION_NOT_READY', 'The saved connection Gateway changed; ask its operator to inspect it.')
            record = {**(record or {}), 'id': connection_id, 'project_id': project_id, 'connector_id': connector_id,
                      'name': template.name, 'kind': template.kind, 'gateway_arn': binding.gateway_arn,
                      'owner': owner, 'inputs': values, 'state': 'PROVISIONING'}
            try:
                self._save(record)
                self._target(template, binding, values, record)
                record['state'] = 'READY'
                self._save(record)
                return _summary(record)
            except Exception as error:
                record['state'] = 'FAILED'
                try:
                    self._save(record)
                except Exception:
                    pass
                if isinstance(error, StoreError):
                    raise
                raise StoreError('CONNECTION_UNAVAILABLE', 'Connection provisioning could not be confirmed; inspect the saved connection before retrying.') from None
