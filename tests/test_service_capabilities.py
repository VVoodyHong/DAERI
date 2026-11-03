"""Public OAuth delegation and authenticated task-file delivery across real boundaries."""

from dataclasses import replace
from datetime import timedelta
import gc
import json
import time
from uuid import uuid4

import anyio
import boto3
from botocore import UNSIGNED
from botocore.awsrequest import AWSResponse
from botocore.config import Config
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult
import pytest

from daeri.application.agent import AgentService
from daeri.application.cloud import CloudWorkflow
from daeri.core.cloud import AccessContext, HarnessReply
from daeri.core.errors import StoreError
from daeri.core.projects import Project
from daeri.infrastructure.agentcore import AgentCoreHarness
from daeri.infrastructure.artifacts import ArtifactService
from daeri.infrastructure.auth import JwtTokenVerifier
from daeri.infrastructure.cloud_s3 import S3CloudStore
from daeri.infrastructure.harness_oauth import OAuthHarnessClient
from daeri.infrastructure.mcp_server import build_mcp
from test_agent_service import PLAN, setup as agent_setup
from test_artifacts import S3 as FileS3
from test_auth import AUDIENCE, ISSUER, RESOURCE, issuer
from test_cloud_s3 import ARN, BUCKET, S3
from test_harness_oauth import Raw, frame
from test_mcp_server import serving


class Catalog:
    def get(self, project_id):
        return Project(project_id, project_id.title(), ARN, 'RELEASE_V2', '2')


def test_signed_public_mcp_token_reaches_native_harness_through_both_worker_paths(issuer):
    client = boto3.client('bedrock-agentcore', region_name='us-east-1',
                         aws_access_key_id='unused', aws_secret_access_key='unused',
                         config=Config(signature_version=UNSIGNED, retries={'total_max_attempts': 1}))
    requests = []
    def send(request):
        requests.append(request)
        wire = frame('messageStart', {'role': 'assistant'})
        if len(requests) == 1:
            wire += frame('contentBlockStart', {'contentBlockIndex': 0, 'start': {'toolUse': {
                'toolUseId': 'actual-plan', 'name': 'confirm_plan', 'type': 'tool_use'}}})
            wire += frame('contentBlockDelta', {'contentBlockIndex': 0, 'delta': {
                'toolUse': {'input': json.dumps({'plan': PLAN})}}})
            wire += frame('contentBlockStop', {'contentBlockIndex': 0})
            wire += frame('messageStop', {'stopReason': 'tool_use'})
        else:
            wire += frame('contentBlockDelta', {'contentBlockIndex': 0, 'delta': {'text': 'Verified result'}})
            wire += frame('contentBlockStop', {'contentBlockIndex': 0})
            wire += frame('messageStop', {'stopReason': 'end_turn'})
        return AWSResponse(request.url, 200, {'content-type': 'application/vnd.amazon.eventstream'}, Raw(wire))
    client._endpoint.http_session.send = send
    s3 = S3()
    file_s3 = FileS3()
    artifacts = ArtifactService(file_s3, BUCKET)
    workflow = CloudWorkflow(S3CloudStore(s3, BUCKET),
                             AgentCoreHarness(OAuthHarnessClient(client, audience=AUDIENCE)), Catalog(),
                             output_location=artifacts.location)
    agent = AgentService(workflow, check_harness=lambda route: None, artifacts=artifacts)
    verifier = JwtTokenVerifier(ISSUER, AUDIENCE, RESOURCE)
    server = build_mcp(agent, verifier, ISSUER, RESOURCE, ['daeri/use'], harness_audience=AUDIENCE)
    access_token = issuer['token']()
    renewed_token = issuer['token']({'exp': int(time.time()) + 1200})

    async def check():
        async def approve(context, params):
            assert params.message == PLAN
            return ElicitResult(action='accept', content={'decision': 'approve'})
        async with serving(server) as url:
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + access_token,
                                                  'Host': 'daeri.example.com'}, timeout=5) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write, elicitation_callback=approve,
                                             read_timeout_seconds=timedelta(seconds=3)) as session:
                        await session.initialize()
                        started = await session.call_tool('daeri_work', {
                            'project_id': 'charging', 'request': 'Compare the cache'})
                        assert not started.isError and started.structuredContent['awaiting_confirmation']
                        task_id = started.structuredContent['task_id']
                        http.headers['Authorization'] = 'Bearer ' + renewed_token
                        done = await session.call_tool('daeri_confirm', {'project_id': 'charging', 'task_id': task_id})
                        assert not done.isError and done.structuredContent['response'] == 'Verified result'
                        assert not done.structuredContent['awaiting_confirmation']
                        task = workflow.get_task(AccessContext('alice', 'charging'), task_id)
                        prefix = artifacts.location(task)['prefix']
                        file_s3.contents = [{'Key': prefix + 'stations.csv', 'Size': 12}]
                        files = await session.call_tool('daeri_files', {'project_id': 'charging', 'task_id': task_id})
                        assert not files.isError
                        download = await session.call_tool('daeri_download', {
                            'project_id': 'charging', 'task_id': task_id,
                            'artifact_id': files.structuredContent['items'][0]['id']})
                        assert not download.isError and download.structuredContent['name'] == 'stations.csv'
                        return task_id
    try:
        task_id = anyio.run(check)
        assert [request.headers['Authorization'] for request in requests] == [
            ('Bearer ' + access_token).encode(), ('Bearer ' + renewed_token).encode()]
        for request in requests:
            body = json.loads(request.body)
            assert body['actorId'] == 'alice'
            assert request.headers['X-Amzn-Bedrock-AgentCore-Runtime-User-Id'] == b'alice'
            assert access_token.encode() not in request.body and renewed_token.encode() not in request.body
        stored = repr(s3.objects)
        assert access_token not in stored and renewed_token not in stored
        # The public request's token context is cleared after either worker path completes.
        task = workflow.get_task(AccessContext('alice', 'charging'), task_id)
        result = workflow.harness.invoke(task, [])
        assert result.error == 'AUTHENTICATION_REQUIRED' and len(requests) == 2
    finally:
        client.close()


def test_agent_files_use_saved_task_ownership_and_output_location_after_a_host_switch():
    agent, user, store, harness = agent_setup(HarnessReply('The CSV is ready', 'end_turn'))
    s3 = FileS3()
    artifacts = ArtifactService(s3, BUCKET)
    agent.artifacts = artifacts
    agent.workflow.output_location = artifacts.location
    result = agent.work(user, 'project', 'Count the stations and create a CSV')
    task = agent.workflow.get_task(user.context('project'), result['task_id'])
    location = artifacts.location(task)
    assert location['uri'] in harness.calls[0][1][0]['content'][-1]['text']
    s3.contents = [{'Key': location['prefix'] + 'reports/충전소.csv', 'Size': 12}]
    other_host = AgentService(agent.workflow, check_harness=lambda route: None, artifacts=artifacts)
    snapshot = dict(store.client.objects)
    files = other_host.files(user, 'project', task.id)
    item = files['items'][0]
    download = other_host.download(user, 'project', task.id, item['id'])
    assert download['name'] == '충전소.csv' and download['expires_at'] <= user.expires_at
    signed = next(call[2] for call in s3.calls if call[0] == 'sign')
    assert signed['Params']['Key'] == location['prefix'] + 'reports/충전소.csv'
    assert signed['Params']['VersionId'] == 'version-one'
    assert store.client.objects == snapshot and len(harness.calls) == 1
    s3.calls.clear()
    for caller, project_id, task_id in [
        (replace(user, actor_id='bob'), 'project', task.id),
        (replace(user, project_ids=frozenset({'project', 'other'})), 'other', task.id),
        (user, 'project', str(uuid4())),
    ]:
        with pytest.raises(StoreError, match='ACCESS_DENIED'):
            other_host.files(caller, project_id, task_id)
        with pytest.raises(StoreError, match='ACCESS_DENIED'):
            other_host.download(caller, project_id, task_id, item['id'])
    assert not s3.calls


def test_failed_oauth_binding_returns_structured_authentication_error_without_leaking_a_coroutine(recwarn):
    from test_mcp_server import Agent, Verifier, connected
    # A verifier result without its delegated audience must fail before starting a worker.
    server = build_mcp(Agent(), Verifier(), 'https://company.example',
                       'https://daeri.example/mcp', ['daeri:use'], harness_audience='daeri-api')
    async def check():
        async with serving(server) as url:
            async with connected(url) as (session, _, __):
                result = await session.call_tool('daeri_projects')
                assert result.isError
                assert result.structuredContent['error']['code'] == 'AUTHENTICATION_REQUIRED'
    anyio.run(check)
    gc.collect()
    assert not [warning for warning in recwarn if issubclass(warning.category, RuntimeWarning)]
