"""Translate the AgentCore InvokeHarness event stream into the cloud port."""

import json

from daeri.core.cloud import HarnessReply


_EVENT_ERRORS = {
    'internalServerException': 'HARNESS_INTERNAL_SERVER_ERROR',
    'validationException': 'HARNESS_VALIDATION_ERROR',
    'runtimeClientError': 'HARNESS_RUNTIME_ERROR',
}
_CLIENT_ERRORS = {
    'AccessDeniedException': 'HARNESS_ACCESS_DENIED',
    'ResourceNotFoundException': 'HARNESS_NOT_FOUND',
    'ThrottlingException': 'HARNESS_THROTTLED',
    'ValidationException': 'HARNESS_VALIDATION_ERROR',
    'ServiceQuotaExceededException': 'HARNESS_QUOTA_EXCEEDED',
}
_INCOMPLETE_STOPS = {
    'max_tokens', 'content_filtered', 'malformed_model_output', 'malformed_tool_use',
    'interrupted', 'partial_turn', 'model_context_window_exceeded',
    'max_iterations_exceeded', 'max_output_tokens_exceeded', 'timeout_exceeded', 'hook_stopped',
}


def _reject_constant(value):
    raise ValueError('Non-JSON numeric constant')


class _HarnessStream:
    def __init__(self):
        self.text = ''
        self.role = None
        self.stop = None
        self.message_open = False
        self.blocks = {}
        self.active = set()
        self.tools = []
        self.observations = []
        self.completed = set()
        self.error = None

    def consume(self, event):
        for name, code in _EVENT_ERRORS.items():
            if name in event:
                self.error = code
                return
        if 'messageStart' in event:
            if self.message_open:
                raise ValueError('Unclosed message')
            self.role = event['messageStart']['role']
            self.message_open, self.stop = True, None
            self.blocks, self.active, self.tools = {}, set(), []
            if self.role == 'assistant':
                self.text = ''
        elif 'contentBlockStart' in event:
            value = event['contentBlockStart']
            index = value['contentBlockIndex']
            if not self.message_open or index in self.active:
                raise ValueError('Invalid content block')
            self.active.add(index)
            start = value['start']
            if 'toolUse' in start:
                self.blocks[index] = {'toolUse': dict(start['toolUse']), 'input': ''}
            elif 'toolResult' in start:
                self.blocks[index] = {'toolResult': {**start['toolResult'], 'content': []}}
        elif 'contentBlockDelta' in event:
            value = event['contentBlockDelta']
            index, delta = value['contentBlockIndex'], value['delta']
            if not self.message_open:
                raise ValueError('Missing message start')
            self.active.add(index)
            if 'text' in delta and self.role == 'assistant':
                self.text += delta['text']
            elif 'toolUse' in delta:
                self.blocks[index]['input'] += delta['toolUse']['input']
            elif 'toolResult' in delta:
                self.blocks[index]['toolResult']['content'].extend(
                    {key: item[key] for key in ('text', 'json') if key in item}
                    for item in delta['toolResult'])
            # Reasoning, tool metadata, and hook payloads are never persisted.
        elif 'contentBlockStop' in event:
            index = event['contentBlockStop']['contentBlockIndex']
            self.active.remove(index)
            block = self.blocks.pop(index, {})
            if 'toolUse' in block:
                self.tools.append(block)
            elif 'toolResult' in block:
                result = block['toolResult']
                self.completed.add(result['toolUseId'])
                self.observations.append(result)
        elif 'messageStop' in event:
            if not self.message_open or self.active:
                raise ValueError('Incomplete content')
            self.stop = event['messageStop']['stopReason']
            self.message_open = False

    def reply(self):
        pending = []
        if not self.error:
            if self.message_open or self.stop is None or self.stop == 'tool_result':
                self.error = 'HARNESS_STREAM_INCOMPLETE'
            elif self.stop in _INCOMPLETE_STOPS:
                self.error = 'HARNESS_' + self.stop.upper()
            elif self.stop == 'tool_use':
                try:
                    for block in self.tools:
                        tool = block['toolUse']
                        if tool['toolUseId'] in self.completed:
                            continue
                        if tool.get('type', 'tool_use') != 'tool_use':
                            if pending:
                                break
                            continue
                        data = json.loads(block['input'] or '{}', parse_constant=_reject_constant)
                        if not isinstance(data, dict):
                            raise ValueError('Tool input must be an object')
                        json.dumps(data, allow_nan=False)
                        pending.append({**tool, 'input': data})
                    if (not pending or self.role != 'assistant'
                            or len({tool['toolUseId'] for tool in pending}) != len(pending)):
                        self.error = 'HARNESS_STREAM_INCOMPLETE'
                except (ValueError, TypeError, RecursionError):
                    self.error = 'HARNESS_INVALID_TOOL_INPUT'
            elif self.stop not in {'end_turn', 'stop_sequence'} or self.role != 'assistant':
                self.error = 'HARNESS_STREAM_INCOMPLETE'
        return HarnessReply(self.text, self.stop or 'failed',
                            tuple(pending) if not self.error else (), tuple(self.observations), self.error)


class AgentCoreHarness:
    """Use a caller-configured SDK client; this adapter never creates credentials or retries."""

    def __init__(self, client, harness_arn, model=None):
        self.client, self.harness_arn, self.model = client, harness_arn, model

    def invoke(self, task, messages):
        state, stream = _HarnessStream(), None
        request = {'harnessArn': self.harness_arn, 'runtimeSessionId': task.session_id,
                   'actorId': task.context.actor_id, 'runtimeUserId': task.context.actor_id,
                   'messages': messages}
        if self.model is not None:
            request['model'] = self.model
        try:
            stream = self.client.invoke_harness(**request)['stream']
            for event in stream:
                state.consume(event)
                if state.error:
                    break
        except Exception as error:
            response = getattr(error, 'response', None)
            details = response.get('Error') if isinstance(response, dict) else None
            code = details.get('Code') if isinstance(details, dict) else None
            state.error = _CLIENT_ERRORS.get(code, 'HARNESS_UNAVAILABLE') if isinstance(code, str) else 'HARNESS_UNAVAILABLE'
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        return state.reply()
