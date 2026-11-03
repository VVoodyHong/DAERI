"""Gateway Lambda agent-as-tool execution; the orchestrator integrates returned results."""

import json
import os
import time
from uuid import uuid4

from daeri.config.delegation import DelegationSettings
from daeri.core.cloud import AccessContext, CloudTask
from daeri.core.errors import StoreError
from daeri.core.serialization import validate_storable_json
from daeri.infrastructure.agentcore import AgentCoreHarness
from daeri.infrastructure.project_catalog import SsmProjectCatalog


def gateway_target(settings):
    tools = [{'name': name, 'description': agent.description,
              'inputSchema': {'type': 'object', 'properties': {
                  'request': {'type': 'string', 'description': 'Delegated goal, approved scope and required evidence.'}},
                  'required': ['request']}}
             for name, agent in settings.subagents.items()]
    return {'gatewayIdentifier': settings.gateway_arn, 'name': settings.target_name,
            'targetConfiguration': {'mcp': {'lambda': {'lambdaArn': settings.lambda_arn,
                                                     'toolSchema': {'inlinePayload': tools}}}},
            'credentialProviderConfigurations': [{'credentialProviderType': 'GATEWAY_IAM_ROLE'}]}


def check_release(control, route):
    endpoint = control.get_harness_endpoint(harnessId=route.harness_arn.rsplit('/', 1)[1],
                                           endpointName=route.harness_endpoint)['endpoint']
    if (endpoint.get('status') != 'READY' or endpoint.get('liveVersion') != route.harness_version
            or endpoint.get('targetVersion') != route.harness_version):
        raise StoreError('HARNESS_NOT_READY', 'The registered subagent release changed or is unavailable.')


def handle_delegation(event, context, settings, target_id, control, harness):
    metadata = getattr(getattr(context, 'client_context', None), 'custom', {})
    prefix = settings.target_name + '___'
    tool = metadata.get('bedrockAgentCoreToolName', '')
    if (metadata.get('bedrockAgentCoreGatewayId') != settings.gateway_arn.rsplit('/', 1)[1]
            or metadata.get('bedrockAgentCoreTargetId') != target_id
            or not tool.startswith(prefix) or tool[len(prefix):] not in settings.subagents):
        raise StoreError('ACCESS_DENIED', 'The delegation is unavailable in this Gateway project scope.')
    if (not isinstance(event, dict) or set(event) != {'request'}
            or not isinstance(event['request'], str) or not event['request'].strip()):
        raise StoreError('INVALID_INPUT', 'Supply the delegated goal and approved scope.')
    validate_storable_json(event)
    name = tool[len(prefix):]
    route = settings.subagents[name]
    check_release(control, route)
    task = CloudTask(str(uuid4()), str(uuid4()), AccessContext('agent:' + settings.project_id + ':' + name,
        settings.project_id), harness_arn=route.harness_arn,
        harness_endpoint=route.harness_endpoint, harness_version=route.harness_version)
    task_context = {'task_id': task.id, 'project_id': task.context.project_id, 'session_id': task.session_id}
    reply = harness.invoke(task, [{'role': 'user', 'content': [{'text': event['request']},
        {'text': 'DAERI delegated task context: ' + json.dumps(task_context)}]}])
    completed = reply.error is None and not reply.pending_tools and reply.stop_reason in {'end_turn', 'stop_sequence'}
    return {'subagent': name, 'task_id': task.id, 'session_id': task.session_id, 'response': reply.text,
            'completed': completed, 'error': reply.error or ('SUBAGENT_INPUT_REQUIRED' if reply.pending_tools else
                                                            None if completed else 'SUBAGENT_INCOMPLETE')}


def configure_subagents(control, lambdas, ssm, settings, project_settings, check_harness):
    """Register the Gateway tool, configure its fixed scope, then publish the root release."""
    from daeri.infrastructure.project_deployment import ProjectDeployment

    if settings.project_id != project_settings.project_id:
        raise StoreError('INVALID_INPUT', 'Use the same project for subagents and their orchestrator.')
    published = SsmProjectCatalog(ssm, project_settings.project_parameter_prefix).get(settings.project_id)
    if published.harness_arn != project_settings.harness_arn or published.name != project_settings.name:
        raise StoreError('PROJECT_NOT_READY', 'The published orchestrator differs from the project deployment.')
    check_harness(published)
    deployed = control.get_harness(harnessId=published.harness_arn.rsplit('/', 1)[1],
                                   harnessVersion=published.harness_version)['harness']
    root = control.get_harness(harnessId=published.harness_arn.rsplit('/', 1)[1])['harness']
    memory = deployed.get('memory', {}).get('agentCoreMemoryConfiguration', {}).get('arn')
    if root.get('memory') != deployed.get('memory'):
        raise StoreError('PROJECT_NOT_READY', 'Publish the project memory changes before configuring subagents.')
    function = lambdas.get_function_configuration(FunctionName=settings.lambda_arn)
    environment = dict(function.get('Environment', {}).get('Variables', {}))
    environment['DAERI_DELEGATION_CONFIG'] = json.dumps({**settings.model_dump(), 'target_id': '0' * 10})
    if sum(len(key.encode()) + len(value.encode()) for key, value in environment.items()) > 4096:
        raise StoreError('INVALID_INPUT', 'The subagent configuration exceeds the Lambda environment limit.')
    for route in settings.subagents.values():
        if route.harness_arn == published.harness_arn:
            raise StoreError('INVALID_INPUT', 'Use a separate managed harness for each delegated role.')
        check_release(control, route)
        child = control.get_harness(harnessId=route.harness_arn.rsplit('/', 1)[1],
                                    harnessVersion=route.harness_version)['harness']
        if (not memory or child.get('memory', {}).get('agentCoreMemoryConfiguration', {}).get('arn') != memory
                or 'actorId' in child.get('memory', {}).get('agentCoreMemoryConfiguration', {})
                or child.get('status') != 'READY' or child.get('timeoutSeconds', 3600) + 15 > min(function['Timeout'], 840)
                or any(tool.get('type') == 'inline_function' for tool in child.get('tools', []))
                or any(hook.get('beforeToolCall', {}).get('target', {}).get('lambda', {}).get('arn')
                       == project_settings.approval_hook_arn for hook in child.get('hooks', []))):
            raise StoreError('PROJECT_NOT_READY', 'Subagents need shared project Memory, bounded execution and server-side tools.')
    request, target_id = gateway_target(settings), None
    query = {'gatewayIdentifier': settings.gateway_arn}
    while True:
        page = control.list_gateway_targets(**query)
        for target in page.get('items', []):
            if target.get('name') == settings.target_name:
                target_id = target['targetId']
        if not page.get('nextToken'):
            break
        query['nextToken'] = page['nextToken']
    if target_id is None:
        target_id = control.create_gateway_target(**request)['targetId']
    else:
        current = control.get_gateway_target(gatewayIdentifier=settings.gateway_arn, targetId=target_id)
        if current.get('targetConfiguration', {}).get('mcp', {}).get('lambda', {}).get('lambdaArn') != settings.lambda_arn:
            raise StoreError('PROJECT_NOT_READY', 'The Gateway target name belongs to another configured function.')
        control.update_gateway_target(**request, targetId=target_id)
    for attempt in range(60):
        target = control.get_gateway_target(gatewayIdentifier=settings.gateway_arn, targetId=target_id)
        if target['status'] == 'READY':
            break
        if target['status'] not in {'CREATING', 'UPDATING'} or attempt == 59:
            raise StoreError('PROJECT_NOT_READY', 'The subagent Gateway target is not ready.')
        time.sleep(1)
    environment['DAERI_DELEGATION_CONFIG'] = json.dumps({**settings.model_dump(), 'target_id': target_id})
    lambdas.update_function_configuration(FunctionName=settings.lambda_arn, Environment={'Variables': environment})
    lambdas.get_waiter('function_updated_v2').wait(FunctionName=settings.lambda_arn)
    tools = root.get('tools', [])
    connected = any(tool.get('config', {}).get('agentCoreGateway', {}).get('gatewayArn') == settings.gateway_arn
                    for tool in tools)
    if not connected:
        control.update_harness(harnessId=published.harness_arn.rsplit('/', 1)[1], tools=tools + [{
            'name': 'project_subagents', 'type': 'agentcore_gateway',
            'config': {'agentCoreGateway': {'gatewayArn': settings.gateway_arn}}}])
    result = ProjectDeployment(control, ssm, check_harness).apply(project_settings)
    return {'target_id': target_id, 'project': result}


def lambda_handler(event, context):
    from contextlib import ExitStack
    import boto3
    from botocore.config import Config
    try:
        saved = json.loads(os.environ['DAERI_DELEGATION_CONFIG'])
        target_id = saved.pop('target_id')
        settings = DelegationSettings.model_validate(saved)
        region = settings.gateway_arn.split(':')[3]
        with ExitStack() as resources:
            control = boto3.client('bedrock-agentcore-control', region_name=region,
                                   config=Config(retries={'total_max_attempts': 1}))
            resources.callback(control.close)
            client = boto3.client('bedrock-agentcore', region_name=region,
                config=Config(retries={'total_max_attempts': 1}, read_timeout=840))
            resources.callback(client.close)
            return handle_delegation(event, context, settings, target_id, control, AgentCoreHarness(client))
    except Exception as error:
        return {'completed': False, 'error': error.code if isinstance(error, StoreError) else 'DELEGATION_FAILED',
                'response': 'The delegated task could not complete; inspect its state before repeating external actions.'}
