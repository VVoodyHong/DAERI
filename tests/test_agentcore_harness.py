"""InvokeHarness fixtures validated against the installed AWS SDK, without AWS calls."""

import json
from uuid import uuid4
from dataclasses import replace

import pytest

import boto3
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

from daeri.core.cloud import AccessContext, CloudTask


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/daeri-abcdefghij'
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
    return CloudTask(str(uuid4()), str(uuid4()), AccessContext('actor-one', 'project'), harness_arn=ARN,
                     harness_endpoint='RELEASE_V2', harness_version='2')


@pytest.mark.parametrize('route', [
    {'harness_endpoint': '', 'harness_version': ''},
    {'harness_endpoint': 'DEFAULT'}, {'harness_endpoint': '2'},
    {'harness_version': ''}, {'harness_version': '0'},
])
def test_unversioned_or_unsafe_route_is_rejected_before_calling_aws(route):
    from daeri.infrastructure.agentcore import AgentCoreHarness
    from daeri.core.errors import StoreError

    client = Client()
    with pytest.raises(StoreError) as error:
        AgentCoreHarness(client).invoke(replace(task(), **route), [])
    assert error.value.code == 'PROJECT_UNAVAILABLE' and client.requests == []


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
    reply = AgentCoreHarness(client).invoke(current, messages)

    assert reply.text == 'Final answer.' and reply.stop_reason == 'end_turn'
    assert reply.error is None and reply.observations == () and reply.pending_tools == ()
    assert 'PRIVATE' not in repr(reply)
    assert client.requests == [{'harnessArn': ARN, 'runtimeSessionId': current.session_id,
                                'qualifier': 'RELEASE_V2',
                                'actorId': current.context.actor_id, 'runtimeUserId': current.context.actor_id,
                                'messages': messages}]
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
    reply = AgentCoreHarness(client).invoke(task(), [{'role': 'user', 'content': [{'text': 'Run'}]}])

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
    reply = AgentCoreHarness(Client(events)).invoke(task(), [])
    assert reply.error is None and reply.pending_tools == ()


@pytest.mark.parametrize('payload', ['{"broken":', '[]', 'null', '{"value":NaN}', '{"value":1e999}'])
def test_malformed_inline_tool_input_cannot_be_executed(payload):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [{'messageStart': {'role': 'assistant'}}, *tool_events('inline', data=payload),
              {'messageStop': {'stopReason': 'tool_use'}}]
    reply = AgentCoreHarness(Client(events)).invoke(task(), [])
    assert reply.error == 'HARNESS_INVALID_TOOL_INPUT' and reply.pending_tools == ()


@pytest.mark.parametrize('reason', ['interrupted', 'partial_turn', 'max_tokens', 'timeout_exceeded',
                                  'guardrail_intervened'])
def test_explicit_stop_reason_is_preserved_as_failure_and_keeps_text(reason):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    reply = AgentCoreHarness(Client(text_events('Partial', reason))).invoke(task(), [])
    assert reply.text == 'Partial' and reply.stop_reason == reason
    assert reply.error == 'HARNESS_' + reason.upper()
    assert reply.pending_tools == ()


@pytest.mark.parametrize('failure', [None, RuntimeError('PRIVATE source and credentials')])
def test_truncated_stream_preserves_partial_text_without_error_details(failure):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    client = Client(text_events('Partial')[:2], failure=failure)
    reply = AgentCoreHarness(client).invoke(task(), [])
    assert reply.text == 'Partial' and reply.error and reply.pending_tools == ()
    assert 'PRIVATE' not in repr(reply) and client.stream.closed


def test_later_open_message_cannot_reuse_previous_success_stop():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    client = Client(text_events('Old') + text_events('New partial')[:2])
    reply = AgentCoreHarness(client).invoke(task(), [])
    assert reply.text == 'New partial' and reply.error


def test_exception_event_after_partial_text_is_sanitized_and_not_retried():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    client = Client(text_events('Partial')[:2] + [{'runtimeClientError': {'message': 'PRIVATE'}}])
    reply = AgentCoreHarness(client).invoke(task(), [])
    assert reply.text == 'Partial' and reply.error == 'HARNESS_RUNTIME_ERROR'
    assert 'PRIVATE' not in repr(reply) and len(client.requests) == 1 and client.stream.closed


def test_native_handoff_returns_only_the_first_inline_call_awaiting_a_response():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [
        {'messageStart': {'role': 'assistant'}},
        *tool_events('first', index=0),
        *tool_events('adjacent', index=1),
        *tool_events('remote', 'server_tool_use', index=2),
        *tool_events('later', index=3),
        {'messageStop': {'stopReason': 'tool_use'}},
    ]
    reply = AgentCoreHarness(Client(events)).invoke(task(), [])
    assert reply.error is None
    assert [tool['toolUseId'] for tool in reply.pending_tools] == ['first']


def test_duplicate_inline_tool_id_cannot_be_handed_off_for_execution_twice():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [{'messageStart': {'role': 'assistant'}},
              *tool_events('duplicate', index=0), *tool_events('duplicate', index=1),
              {'messageStop': {'stopReason': 'tool_use'}}]
    client = Client(events)
    reply = AgentCoreHarness(client).invoke(task(), [])
    assert reply.error and reply.pending_tools == () and client.stream.closed


def test_incomplete_remote_result_is_not_a_completed_observation():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    events = [
        {'messageStart': {'role': 'user'}},
        {'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolResult': {
            'toolUseId': 'remote', 'status': 'success'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'toolResult': [{'text': 'Partial'}]}}},
    ]
    reply = AgentCoreHarness(Client(events)).invoke(task(), [])
    assert reply.error and reply.observations == () and reply.pending_tools == ()


def test_stream_close_failure_preserves_completed_reply():
    from daeri.infrastructure.agentcore import AgentCoreHarness

    class CloseFailure(Events):
        def close(self):
            self.closed = True
            raise RuntimeError('PRIVATE transport details')
    client = Client()
    client.stream = CloseFailure(text_events())
    reply = AgentCoreHarness(client).invoke(task(), [])
    assert reply.text == 'Completed.' and reply.stop_reason == 'end_turn' and reply.error is None
    assert client.stream.closed and 'PRIVATE' not in repr(reply)


@pytest.mark.parametrize('response', [None, {'Error': None}, {'Error': {'Code': ['PRIVATE']}}])
def test_non_sdk_exception_response_cannot_escape_sanitized_boundary(response):
    from daeri.infrastructure.agentcore import AgentCoreHarness

    failure = RuntimeError('PRIVATE transport details')
    failure.response = response
    reply = AgentCoreHarness(Client(invoke_failure=failure)).invoke(task(), [])
    assert reply.error == 'HARNESS_UNAVAILABLE' and 'PRIVATE' not in repr(reply)


def test_missing_saved_route_never_invokes_an_arbitrary_harness():
    from dataclasses import replace
    from daeri.infrastructure.agentcore import AgentCoreHarness
    from daeri.core.errors import StoreError

    client = Client()
    with pytest.raises(StoreError, match='PROJECT_UNAVAILABLE'):
        AgentCoreHarness(client).invoke(replace(task(), harness_arn=''), [])
    assert client.requests == []


@pytest.mark.parametrize('code, expected', [
    ('AccessDeniedException', 'HARNESS_ACCESS_DENIED'),
    ('ResourceNotFoundException', 'HARNESS_NOT_FOUND'),
    ('ThrottlingException', 'HARNESS_THROTTLED'),
    ('ValidationException', 'HARNESS_VALIDATION_ERROR'),
    ('ServiceQuotaExceededException', 'HARNESS_QUOTA_EXCEEDED'),
])
def test_rejected_invocation_returns_contract_error_and_preserves_pending_calls(code, expected):
    from dataclasses import replace
    from daeri.infrastructure.agentcore import AgentCoreHarness

    pending = ({'toolUseId': 'approve', 'name': 'confirm_plan', 'input': {'plan': 'Inspect'}},)
    failure = ClientError({'Error': {'Code': code, 'Message': 'PRIVATE'}}, 'InvokeHarness')
    client = Client(invoke_failure=failure)
    reply = AgentCoreHarness(client).invoke(replace(task(), pending_tools=pending), [])
    assert reply.error == expected and reply.pending_tools == pending
    assert len(client.requests) == 1
    assert 'PRIVATE' not in repr(reply)


def test_unconfirmed_transport_failure_keeps_no_actionable_handoff():
    from dataclasses import replace
    from daeri.infrastructure.agentcore import AgentCoreHarness

    pending = ({'toolUseId': 'approve', 'name': 'confirm_plan', 'input': {}},)
    reply = AgentCoreHarness(Client(invoke_failure=TimeoutError('PRIVATE'))).invoke(
        replace(task(), pending_tools=pending), [])
    assert reply.error == 'HARNESS_UNAVAILABLE' and reply.pending_tools == ()


@pytest.mark.parametrize('location', ['text', 'pending_key', 'pending_value',
                                     'observation_key', 'observation_value'])
@pytest.mark.parametrize('invalid,escaped', [('\ud800', '확보\\ud800텍스트')])
def test_invalid_output_cannot_handoff_tools_or_prevent_saving_valid_evidence(location, invalid, escaped):
    from daeri.infrastructure.agentcore import AgentCoreHarness
    from daeri.core.serialization import validate_storable_json

    dirty = '확보' + invalid + '텍스트'
    events = [
        {'messageStart': {'role': 'user'}},
        {'contentBlockStart': {'contentBlockIndex': 0, 'start': {'toolResult': {
            'toolUseId': 'completed', 'status': 'success'}}}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {'toolResult': [{'text': 'Safe evidence.'}]}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
    ]
    if location.startswith('observation'):
        payload = {dirty: 'value'} if location.endswith('key') else {'value': [dirty]}
        events += [
            {'contentBlockStart': {'contentBlockIndex': 1, 'start': {'toolResult': {
                'toolUseId': 'invalid', 'status': 'success'}}}},
            {'contentBlockDelta': {'contentBlockIndex': 1, 'delta': {'toolResult': [{'json': {'nested': payload}}]}}},
            {'contentBlockStop': {'contentBlockIndex': 1}},
        ]
    events += [
        {'messageStop': {'stopReason': 'tool_result'}},
        {'messageStart': {'role': 'assistant'}},
        {'contentBlockDelta': {'contentBlockIndex': 0, 'delta': {
            'text': dirty if location == 'text' else 'Valid answer.'}}},
        {'contentBlockStop': {'contentBlockIndex': 0}},
    ]
    payload = {dirty: 'value'} if location == 'pending_key' else {
        'value': [dirty] if location == 'pending_value' else ['valid']}
    events += tool_events('inline', index=1, data=json.dumps({'nested': payload}))
    events += [{'messageStop': {'stopReason': 'tool_use'}}]

    reply = AgentCoreHarness(Client(events)).invoke(task(), [])

    assert reply.error == 'HARNESS_INVALID_OUTPUT' and reply.pending_tools == ()
    assert reply.text == (escaped if location == 'text' else 'Valid answer.')
    assert reply.observations == ({'toolUseId': 'completed', 'status': 'success',
                                   'content': [{'text': 'Safe evidence.'}]},)
    validate_storable_json(reply.text)
    validate_storable_json(reply.observations)
