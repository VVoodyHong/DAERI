"""OAuth uses the native SDK wire request and event-stream decoder."""

import asyncio
from io import BytesIO
import json
import struct
import time
from zlib import crc32

import boto3
from botocore import UNSIGNED
from botocore.awsrequest import AWSResponse
from botocore.config import Config
import pytest

from daeri.core.access import AuthenticatedUser
from daeri.core.errors import StoreError


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/daeri-abcdefghij'


def frame(event, payload):
    headers = b''
    for key, value in ((':message-type', 'event'), (':event-type', event), (':content-type', 'application/json')):
        name, value = key.encode(), value.encode()
        headers += bytes([len(name)]) + name + b'\x07' + struct.pack('>H', len(value)) + value
    body = json.dumps(payload).encode()
    prelude = struct.pack('>II', 16 + len(headers) + len(body), len(headers))
    message = prelude + struct.pack('>I', crc32(prelude)) + headers + body
    return message + struct.pack('>I', crc32(message))


class Raw(BytesIO):
    def stream(self, amt=None, decode_content=False):
        yield self.read()


def setup():
    from daeri.infrastructure.harness_oauth import OAuthHarnessClient
    client = boto3.client('bedrock-agentcore', region_name='us-east-1',
                         aws_access_key_id='unused', aws_secret_access_key='unused',
                         config=Config(signature_version=UNSIGNED, retries={'total_max_attempts': 1}))
    requests = []
    def send(request):
        requests.append(request)
        wire = frame('messageStart', {'role': 'assistant'})
        wire += frame('contentBlockDelta', {'contentBlockIndex': 0, 'delta': {'text': 'Completed'}})
        wire += frame('contentBlockStop', {'contentBlockIndex': 0})
        wire += frame('messageStop', {'stopReason': 'end_turn'})
        return AWSResponse(request.url, 200, {'content-type': 'application/vnd.amazon.eventstream'}, Raw(wire))
    client._endpoint.http_session.send = send
    return OAuthHarnessClient(client, audience='daeri-api'), client, requests


def user(actor='alice'):
    return AuthenticatedUser(actor, frozenset({'project'}), int(time.time()) + 3600)


def request(actor='alice'):
    return {'harnessArn': ARN, 'runtimeSessionId': '12345678-1234-1234-1234-123456789012',
            'qualifier': 'RELEASE_V2', 'actorId': actor, 'runtimeUserId': actor,
            'messages': [{'role': 'user', 'content': [{'text': 'Read the project'}]}]}


def test_verified_bearer_reaches_native_sdk_and_event_stream_is_decoded():
    from daeri.infrastructure.harness_oauth import bind_harness_identity
    wrapped, client, requests = setup()
    try:
        with bind_harness_identity(user(), 'private-access-token', 'daeri-api'):
            events = list(wrapped.invoke_harness(**request())['stream'])
        assert events[0] == {'messageStart': {'role': 'assistant'}}
        assert requests[0].headers['Authorization'] == b'Bearer private-access-token'
        assert b'private-access-token' not in requests[0].body
        assert requests[0].url.startswith('https://bedrock-agentcore.us-east-1.amazonaws.com/harnesses/invoke?')
    finally:
        client.close()


@pytest.mark.parametrize('actor,audience', [('bob', 'daeri-api'), ('alice', 'other-api')])
def test_mismatched_actor_or_audience_cannot_forward_a_token(actor, audience):
    from daeri.infrastructure.harness_oauth import bind_harness_identity
    wrapped, client, requests = setup()
    try:
        with bind_harness_identity(user(), 'private-access-token', audience):
            with pytest.raises(StoreError, match='AUTHENTICATION_REQUIRED') as error:
                wrapped.invoke_harness(**request(actor))
        assert not requests and 'private-access-token' not in str(error.value)
    finally:
        client.close()


def test_context_is_reset_and_async_worker_receives_only_its_current_identity():
    from daeri.infrastructure.harness_oauth import bind_harness_identity
    wrapped, client, requests = setup()
    async def invoke(actor):
        with bind_harness_identity(user(actor), 'token-' + actor, 'daeri-api'):
            events = await asyncio.to_thread(lambda: list(wrapped.invoke_harness(**request(actor))['stream']))
        return events
    try:
        asyncio.run(invoke('bob'))
        with pytest.raises(StoreError, match='AUTHENTICATION_REQUIRED'):
            wrapped.invoke_harness(**request('bob'))
        assert requests[0].headers['Authorization'] == b'Bearer token-bob'
    finally:
        client.close()


def test_missing_delegation_token_preserves_the_native_pending_call_without_sending():
    from dataclasses import replace
    from daeri.infrastructure.agentcore import AgentCoreHarness
    from test_agentcore_harness import task
    wrapped, client, requests = setup()
    pending = {'toolUseId': 'plan', 'name': 'confirm_plan', 'input': {'plan': 'Read the project'}}
    try:
        result = AgentCoreHarness(wrapped).invoke(replace(task(), pending_tools=(pending,)), [])
        assert result.error == 'AUTHENTICATION_REQUIRED' and result.pending_tools == (pending,)
        assert not requests
    finally:
        client.close()
