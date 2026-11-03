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


def _ready(client, runtime_id, *, endpoint=None):
    deadline = time.monotonic() + 180
    while True:
        result = (client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=endpoint)
                  if endpoint else client.get_agent_runtime(agentRuntimeId=runtime_id))
        if result['status'] == 'READY':
            return result
        if result['status'] not in {'CREATING', 'UPDATING'} or time.monotonic() >= deadline:
            raise StoreError('RUNTIME_NOT_READY', 'Check the saved Runtime deployment before retrying.')
        time.sleep(1)


def configure_runtime(client, settings):
    """Create/update hosting, then publish its configured version through an endpoint."""
    runtime_id = settings.runtime_id
    try:
        if runtime_id is None:
            # No endpoint is published until the returned ARN is in the final configuration.
            created = client.create_agent_runtime(agentRuntimeName=settings.name, **runtime_request(settings))
            runtime_id = created['agentRuntimeId']
        current = _ready(client, runtime_id)
        url = runtime_endpoint(current['agentRuntimeArn'], settings.endpoint)
        client.update_agent_runtime(agentRuntimeId=runtime_id, **runtime_request(settings, public_url=url))
        deployed = _ready(client, runtime_id)
        try:
            client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=settings.endpoint)
        except Exception as error:
            code = getattr(error, 'response', {}).get('Error', {}).get('Code')
            if code != 'ResourceNotFoundException':
                raise
            client.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name=settings.endpoint,
                                                 agentRuntimeVersion=deployed['agentRuntimeVersion'])
        else:
            client.update_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=settings.endpoint,
                                                 agentRuntimeVersion=deployed['agentRuntimeVersion'])
        endpoint = _ready(client, runtime_id, endpoint=settings.endpoint)
        if endpoint.get('liveVersion') != deployed['agentRuntimeVersion'] or endpoint.get('targetVersion') != deployed['agentRuntimeVersion']:
            raise StoreError('RUNTIME_NOT_READY', 'The public endpoint does not match the configured Runtime version.')
        return {'runtime_id': runtime_id, 'runtime_arn': deployed['agentRuntimeArn'],
                'version': deployed['agentRuntimeVersion'], 'public_url': url}
    except Exception as error:
        failure = error if isinstance(error, StoreError) else StoreError(
            'DEPLOYMENT_FAILED', 'Runtime configuration could not be confirmed; inspect the deployment before retrying.')
        failure.runtime_id = runtime_id
        raise failure from None
