"""InvokeHarness fixtures validated against the installed AWS SDK, without AWS calls."""

from uuid import uuid4

import pytest

boto3 = pytest.importorskip('boto3', reason='AWS contract tests require the cloud extra')
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

from daeri.core.cloud import AccessContext, CloudTask


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/daeri-abcdefghij'
MODEL = {'bedrockModelConfig': {'modelId': 'example.model-v1', 'apiFormat': 'converse_stream'}}
SERVICE = boto3.Session()._session.get_service_model('bedrock-agentcore')
OPERATION = SERVICE.operation_model('InvokeHarness')


class Events:
    def __init__(self, events, failure=None):
        self.events, self.failure, self.closed = events, failure, False

    def __iter__(self):
        for event in self.events:
            validate_parameters(event, OPERATION.output_shape.members['stream'])
            yield event
        if self.failure:
            raise self.failure

    def close(self):
        self.closed = True


class Client:
    def __init__(self, events=(), failure=None, invoke_failure=None):
        self.stream = Events(events, failure)
        self.requests = []
        self.invoke_failure = invoke_failure

    def invoke_harness(self, **kwargs):
        validate_parameters(kwargs, OPERATION.input_shape)
        self.requests.append(kwargs)
        if self.invoke_failure:
            raise self.invoke_failure
        return {'stream': self.stream}


def task():
    return CloudTask(str(uuid4()), str(uuid4()), AccessContext('actor-one', 'project', 'service', frozenset({'source'})))


def text_events(text='Completed.', reason='end_turn'):
    return [
        {'messageStart': {'role': 'assistant'}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'text': text}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
        {'messageStop': {'stopReason': reason}},
    ]


def tool_events(tool_id, kind='tool_use', index=0, data='{"query":"issue"}'):
    return [
        {'contentBlockStart': {'contentBlockIndex': index, 'start': {'toolUse': {
            'toolUseId': tool_id, 'name': 'search', 'type': kind}}}},
        {'contentBlockDelta': {'contentBlockIndex': index, 'delta': {'toolUse': {'input': data}}}},
        {'contentBlockStop': {'contentBlockIndex': index}},
    ]


def test_native_sdk_request_and_final_text_exclude_reasoning_and_close_stream():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    client = Client(text_events('First') + [
        {'messageStart': {'role': 'assistant'}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'reasoningContent': {'text': 'PRIVATE'}}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
        *text_events('Final answer.')[1:],
        {'metadata': {'usage': {'inputTokens': 1, 'outputTokens': 2, 'totalTokens': 3},
                      'metrics': {'latencyMs': 15}}},
    ])
    current = task()
    messages = [{'role': 'user', 'content': [{'text': 'Question'}]}]
    reply = AgentCoreHarness(client, ARN, MODEL).invoke(current, messages)

    assert reply.text == 'Final answer.' and reply.stop_reason == 'end_turn'
    assert reply.error is None and reply.observations == () and reply.pending_tools == ()
    assert 'PRIVATE' not in repr(reply)
    assert client.requests == [{'harnessArn': ARN, 'runtimeSessionId': current.session_id,
                                'actorId': current.context.actor_id, 'runtimeUserId': current.context.actor_id,
                                'model': MODEL, 'messages': messages}]
    assert client.stream.closed


def test_only_final_inline_handoff_is_pending_after_remote_tool_result_with_reused_indices():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [
        {'messageStart': {'role': 'assistant'}},
        *tool_events('remote', 'mcp_tool_use'),
        {'messageStop': {'stopReason': 'tool_use'}},
        {'messageStart': {'role': 'user'}},
        {'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolResult': {
            'toolUseId': 'remote', 'status': 'success'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'toolResult': [
            {'text': 'Issue located.'}, {'json': {'issue': 42}}]}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
        {'messageStop': {'stopReason': 'tool_result'}},
        {'messageStart': {'role': 'assistant'}},
        *tool_events('inline', data='{"approved":true}'),
        {'messageStop': {'stopReason': 'tool_use'}},
    ]
    client = Client(events)
    reply = AgentCoreHarness(client, ARN).invoke(task(), [{'role': 'user', 'content': [{'text': 'Run'}]}])

    assert reply.error is None and reply.stop_reason == 'tool_use'
    assert reply.pending_tools == ({'name': 'search', 'toolUseId': 'inline',
                                    'type': 'tool_use', 'input': {'approved': True}},)
    assert reply.observations == ({'toolUseId': 'remote', 'status': 'success',
                                   'content': [{'text': 'Issue located.'}, {'json': {'issue': 42}}]},)
    assert 'model' not in client.requests[0]
    assert client.stream.closed


@pytest.mark.parametrize('kind', ['tool_use', 'server_tool_use', 'mcp_tool_use'])
def test_tools_executed_by_harness_are_not_pending(kind):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [{'messageStart': {'role': 'assistant'}}, *tool_events('remote', kind),
              {'messageStop': {'stopReason': 'tool_use'}}, *text_events()]
    reply = AgentCoreHarness(Client(events), ARN).invoke(task(), [])
    assert reply.error is None and reply.pending_tools == ()


@pytest.mark.parametrize('payload', ['{"broken":', '[]', 'null', '{"value":NaN}', '{"value":1e999}'])
def test_malformed_inline_tool_input_cannot_be_executed(payload):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [{'messageStart': {'role': 'assistant'}}, *tool_events('inline', data=payload),
              {'messageStop': {'stopReason': 'tool_use'}}]
    reply = AgentCoreHarness(Client(events), ARN).invoke(task(), [])
    assert reply.error == 'HARNESS_INVALID_TOOL_INPUT' and reply.pending_tools == ()


@pytest.mark.parametrize('reason', ['interrupted', 'partial_turn', 'max_tokens', 'timeout_exceeded'])
def test_explicit_incomplete_stop_is_failure_and_keeps_partial_text(reason):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    reply = AgentCoreHarness(Client(text_events('Partial', reason)), ARN).invoke(task(), [])
    assert reply.text == 'Partial' and reply.stop_reason == reason and reply.error
    assert reply.pending_tools == ()


@pytest.mark.parametrize('failure', [None, RuntimeError('PRIVATE source and credentials')])
def test_truncated_stream_preserves_partial_text_without_error_details(failure):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    client = Client(text_events('Partial')[:2], failure=failure)
    reply = AgentCoreHarness(client, ARN).invoke(task(), [])
    assert reply.text == 'Partial' and reply.error and reply.pending_tools == ()
    assert 'PRIVATE' not in repr(reply) and client.stream.closed


def test_later_open_message_cannot_reuse_previous_success_stop():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    client = Client(text_events('Old') + text_events('New partial')[:2])
    reply = AgentCoreHarness(client, ARN).invoke(task(), [])
    assert reply.text == 'New partial' and reply.error


def test_exception_event_after_partial_text_is_sanitized_and_not_retried():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    client = Client(text_events('Partial')[:2] + [{'runtimeClientError': {'message': 'PRIVATE'}}])
    reply = AgentCoreHarness(client, ARN).invoke(task(), [])
    assert reply.text == 'Partial' and reply.error == 'HARNESS_RUNTIME_ERROR'
    assert 'PRIVATE' not in repr(reply) and len(client.requests) == 1 and client.stream.closed


def test_sdk_client_error_is_sanitized_without_retry():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    failure = ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'PRIVATE'}}, 'InvokeHarness')
    client = Client(invoke_failure=failure)
    reply = AgentCoreHarness(client, ARN).invoke(task(), [])
    assert reply.error == 'HARNESS_ACCESS_DENIED' and len(client.requests) == 1
    assert 'PRIVATE' not in repr(reply)


def test_inline_handoff_stops_at_next_harness_executed_tool():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [
        {'messageStart': {'role': 'assistant'}},
        *tool_events('first', index=0),
        *tool_events('adjacent', index=1),
        *tool_events('remote', 'server_tool_use', index=2),
        *tool_events('later', index=3),
        {'messageStop': {'stopReason': 'tool_use'}},
    ]
    reply = AgentCoreHarness(Client(events), ARN).invoke(task(), [])
    assert reply.error is None
    assert [tool['toolUseId'] for tool in reply.pending_tools] == ['first', 'adjacent']


def test_duplicate_inline_tool_id_cannot_be_handed_off_for_execution_twice():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [{'messageStart': {'role': 'assistant'}},
              *tool_events('duplicate', index=0), *tool_events('duplicate', index=1),
              {'messageStop': {'stopReason': 'tool_use'}}]
    client = Client(events)
    reply = AgentCoreHarness(client, ARN).invoke(task(), [])
    assert reply.error and reply.pending_tools == () and client.stream.closed


def test_incomplete_remote_result_is_not_a_completed_observation():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [
        {'messageStart': {'role': 'user'}},
        {'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolResult': {
            'toolUseId': 'remote', 'status': 'success'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'toolResult': [{'text': 'Partial'}]}}},
    ]
    reply = AgentCoreHarness(Client(events), ARN).invoke(task(), [])
    assert reply.error and reply.observations == () and reply.pending_tools == ()


def test_stream_close_failure_preserves_completed_reply():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    class CloseFailure(Events):
        def close(self):
            self.closed = True
            raise RuntimeError('PRIVATE transport details')
    client = Client()
    client.stream = CloseFailure(text_events())
    reply = AgentCoreHarness(client, ARN).invoke(task(), [])
    assert reply.text == 'Completed.' and reply.stop_reason == 'end_turn' and reply.error is None
    assert client.stream.closed and 'PRIVATE' not in repr(reply)


@pytest.mark.parametrize('response', [None, {'Error': None}, {'Error': {'Code': ['PRIVATE']}}])
def test_non_sdk_exception_response_cannot_escape_sanitized_boundary(response):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    failure = RuntimeError('PRIVATE transport details')
    failure.response = response
    reply = AgentCoreHarness(Client(invoke_failure=failure), ARN).invoke(task(), [])
    assert reply.error == 'HARNESS_UNAVAILABLE' and 'PRIVATE' not in repr(reply)
