"""Configure the MCP hosting boundary with native AgentCore Runtime APIs."""

import json
import time

from daeri.core.endpoints import runtime_endpoint
from daeri.core.errors import StoreError


def runtime_request(settings, *, public_url=None):
    auth = settings.server.auth
    request = {
        'agentRuntimeArtifact': {'containerConfiguration': {'containerUri': settings.image_uri}},
        'roleArn': settings.role_arn,
        'networkConfiguration': settings.network_configuration,
        'protocolConfiguration': {'serverProtocol': 'MCP'},
        'authorizerConfiguration': {'customJWTAuthorizer': {
            'discoveryUrl': auth.issuer.rstrip('/') + '/.well-known/openid-configuration',
            'allowedAudience': [auth.audience], 'allowedClients': settings.client_ids,
            'allowedScopes': auth.required_scopes,
        }},
        # The application validates project grants and MCP authority from this same token.
        'requestHeaderConfiguration': {'requestHeaderAllowlist': ['Authorization']},
        'environmentVariables': {},
    }
    if public_url is not None:
        request['environmentVariables']['DAERI_SERVER_CONFIG'] = json.dumps(
            {**settings.server.model_dump(), 'public_url': public_url}, separators=(',', ':'))
    return request


def _ready(client, runtime_id, *, endpoint=None, deadline=None, version=None):
    deadline = time.monotonic() + 180 if deadline is None else deadline
    request = {'agentRuntimeId': runtime_id}
    if endpoint:
        request['endpointName'] = endpoint
        describe = client.get_agent_runtime_endpoint
        version_fields = ('liveVersion', 'targetVersion')
    else:
        if version is not None:
            request['agentRuntimeVersion'] = version
        describe = client.get_agent_runtime
        version_fields = ('agentRuntimeVersion',)
    while True:
        result = describe(**request)
        status = result['status']
        if status == 'READY' and (version is None or all(result.get(field) == version for field in version_fields)):
            return result
        if status not in {'CREATING', 'UPDATING', 'READY'} or time.monotonic() >= deadline:
            raise StoreError('RUNTIME_NOT_READY', 'Check the saved Runtime deployment before retrying.')
        time.sleep(1)


def configure_runtime(client, settings):
    """Configure hosting and confirm its published endpoint version."""
    runtime_id = settings.runtime_id
    try:
        if runtime_id is None:
            created = client.create_agent_runtime(agentRuntimeName=settings.name, **runtime_request(settings))
            runtime_id = created['agentRuntimeId']
        current = _ready(client, runtime_id)
        url = runtime_endpoint(current['agentRuntimeArn'], settings.endpoint)
        updated = client.update_agent_runtime(agentRuntimeId=runtime_id, metadataConfiguration={'requireMMDSV2': True},
                                              **runtime_request(settings, public_url=url))
        version = updated['agentRuntimeVersion']
        deployed = _ready(client, runtime_id, version=version)
        if (deployed.get('metadataConfiguration') or {}).get('requireMMDSV2') is not True:
            raise StoreError('RUNTIME_NOT_READY', 'The Runtime requires confirmed MMDSv2 before publishing its endpoint.')
        # AWS advances DEFAULT automatically when a Runtime version is created.
        if settings.endpoint != 'DEFAULT':
            try:
                client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=settings.endpoint)
            except Exception as error:
                code = getattr(error, 'response', {}).get('Error', {}).get('Code')
                if code != 'ResourceNotFoundException':
                    raise
                client.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name=settings.endpoint,
                                                     agentRuntimeVersion=version)
            else:
                client.update_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=settings.endpoint,
                                                     agentRuntimeVersion=version)
        endpoint = _ready(client, runtime_id, endpoint=settings.endpoint,
                          version=version if settings.endpoint == 'DEFAULT' else None)
        if endpoint.get('liveVersion') != version or endpoint.get('targetVersion') != version:
            raise StoreError('RUNTIME_NOT_READY', 'The public endpoint does not match the configured Runtime version.')
        return {'runtime_id': runtime_id, 'runtime_arn': deployed['agentRuntimeArn'],
                'version': version, 'public_url': url}
    except Exception as error:
        failure = error if isinstance(error, StoreError) else StoreError(
            'DEPLOYMENT_FAILED', 'Runtime configuration could not be confirmed; inspect the deployment before retrying.')
        failure.runtime_id = runtime_id
        raise failure from None
