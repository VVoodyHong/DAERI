"""Workflow policy with real use cases and external harness/storage boundaries."""

from copy import deepcopy
from dataclasses import replace

import pytest

from daeri.application.cloud import CloudWorkflow
from daeri.core.cloud import AccessContext, CloudTask, CloudTurn, HarnessReply
from daeri.core.errors import StoreError
from daeri.core.projects import Project


CONTEXT = AccessContext('alice', 'charging')
PENDING = {'toolUseId': 'approval-1', 'name': 'confirm_plan',
           'input': {'plan': 'Inspect cache'}, 'type': 'tool_use'}
RESULT = {'toolUseId': 'approval-1', 'status': 'success',
          'content': [{'json': {'decision': 'approve', 'response': 'Please proceed'}}]}


class Catalog:
    def __init__(self):
        self.routes = {}

    def get(self, project_id):
        return Project(project_id, project_id.title(), self.routes.get(project_id, 'harness-' + project_id))


class Store:
    def __init__(self):
        self.tasks, self.turns, self.busy = {}, {}, set()
        self.fail_capture = False

    def create_task(self, context, harness_arn, goal):
        key = str(len(self.tasks) + 1)
        task = CloudTask(key, 'session-' + key, context, goal=goal, harness_arn=harness_arn)
        self.tasks[key] = task
        return task

    def get_task(self, context, task_id):
        task = self.tasks[task_id]
        if task.context != context:
            raise StoreError('ACCESS_DENIED', 'Task context mismatch')
        return task

    def begin_turn(self, task, prompt, tool_results):
        if task.id in self.busy:
            raise StoreError('TASK_BUSY', 'Already running')
        self.busy.add(task.id)
        return 'turn-' + str(len(self.turns) + 1)

    def finish_turn(self, task, turn_id, prompt, reply, tool_results):
        if self.fail_capture:
            raise StoreError('STORAGE_UNAVAILABLE', 'Could not capture the result')
        self.turns[task.id] = deepcopy((turn_id, prompt, reply, tool_results))
        self.tasks[task.id] = replace(task, pending_tools=reply.pending_tools)
        self.busy.remove(task.id)

    def recover_turn(self, context, task_id):
        self.get_task(context, task_id)
        turn_id, _, reply, _ = self.turns[task_id]
        return CloudTurn(task_id, turn_id, reply)


class Harness:
    def __init__(self, store, *replies):
        self.store, self.replies, self.calls = store, list(replies), []

    def invoke(self, task, messages):
        assert task.id in self.store.busy, 'An external call requires a durable turn claim'
        self.calls.append((task, deepcopy(messages)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def app(*replies):
    store, catalog = Store(), Catalog()
    harness = Harness(store, *replies)
    return CloudWorkflow(store, harness, catalog), store, harness, catalog


def test_native_memory_harness_receives_only_actual_user_input():
    workflow, store, harness, _ = app(HarnessReply('First result', 'end_turn'),
                                    HarnessReply('Next result', 'end_turn'))
    first = workflow.start(CONTEXT, 'Inspect cache')
    second = workflow.start(replace(CONTEXT, actor_id='bob'), 'Use the earlier experience')
    assert first.task_id != second.task_id
    assert harness.calls[0][1] == [{'role': 'user', 'content': [{'text': 'Inspect cache'}]}]
    assert harness.calls[1][0].context == AccessContext('bob', 'charging')
    assert harness.calls[1][0].session_id != harness.calls[0][0].session_id
    assert store.tasks[first.task_id].goal == 'Inspect cache'


def test_resume_from_another_client_preserves_actual_approval_and_original_route():
    workflow, store, harness, catalog = app(HarnessReply('Approve this plan', 'tool_use', (PENDING,)),
                                          HarnessReply('Done', 'end_turn'))
    first = workflow.start(CONTEXT, 'Inspect cache')
    catalog.routes['charging'] = 'new-harness'
    supplied = deepcopy(RESULT)
    other_client = CloudWorkflow(store, harness, catalog)
    resumed = other_client.resume(CONTEXT, first.task_id, 'Use current data', (supplied,))
    task, messages = harness.calls[1]
    assert task.harness_arn == 'harness-charging'
    assert task.session_id == harness.calls[0][0].session_id
    assert messages == [{'role': 'user', 'content': [{'text': 'Use current data'},
                          {'toolResult': {**RESULT, 'type': 'tool_use'}}]}]
    assert 'type' not in supplied
    assert store.turns[first.task_id][3] == (RESULT,)
    assert resumed.reply.text == 'Done'


def test_tool_responses_follow_saved_pending_order():
    pending2, result2 = {**PENDING, 'toolUseId': 'approval-2'}, {**RESULT, 'toolUseId': 'approval-2'}
    workflow, _, harness, _ = app(HarnessReply('Approve', 'tool_use', (PENDING, pending2)),
                                 HarnessReply('Done', 'end_turn'))
    first = workflow.start(CONTEXT, 'Inspect both')
    workflow.resume(CONTEXT, first.task_id, tool_results=(result2, RESULT))
    blocks = harness.calls[1][1][0]['content']
    assert [block['toolResult']['toolUseId'] for block in blocks] == ['approval-1', 'approval-2']


@pytest.mark.parametrize('results', [(), (RESULT, RESULT), ({**RESULT, 'toolUseId': 'other'},),
    ({**RESULT, 'status': 'unknown'},), ({**RESULT, 'extra': 'untrusted'},),
    ({**RESULT, 'content': [{'text': 3}]},), ({**RESULT, 'content': [{'json': float('nan')}]},),
    ({**RESULT, 'content': [{'json': {2: 'non-string key'}}]},)])
def test_rejected_pending_response_preserves_the_original_wait(results):
    workflow, store, harness, _ = app(HarnessReply('Approve', 'tool_use', (PENDING,)))
    first = workflow.start(CONTEXT, 'Inspect cache')
    with pytest.raises(StoreError) as error:
        workflow.resume(CONTEXT, first.task_id, tool_results=results)
    assert error.value.code == 'INVALID_INPUT'
    assert len(harness.calls) == 1
    assert store.tasks[first.task_id].pending_tools == (PENDING,)
    assert not store.busy


@pytest.mark.parametrize('changed', [{'actor_id': 'bob'}, {'project_id': 'other'}])
def test_another_identity_or_project_cannot_resume_or_recover_a_task(changed):
    workflow, _, harness, _ = app(HarnessReply('Saved', 'end_turn'))
    first = workflow.start(CONTEXT, 'Inspect cache')
    for action in (lambda: workflow.resume(replace(CONTEXT, **changed), first.task_id, 'Continue'),
                   lambda: workflow.recover(replace(CONTEXT, **changed), first.task_id)):
        with pytest.raises(StoreError) as error:
            action()
        assert error.value.code == 'ACCESS_DENIED'
    assert len(harness.calls) == 1


def test_recovery_returns_saved_output_without_calling_the_harness():
    workflow, _, harness, _ = app(HarnessReply('Saved result', 'end_turn'))
    first = workflow.start(CONTEXT, 'Inspect cache')
    assert workflow.recover(CONTEXT, first.task_id) == first
    assert len(harness.calls) == 1


def test_capture_failure_keeps_the_turn_busy_and_prevents_an_external_retry():
    workflow, store, harness, _ = app(HarnessReply('Side effect finished', 'end_turn'))
    store.fail_capture = True
    with pytest.raises(StoreError) as capture:
        workflow.start(CONTEXT, 'Perform approved work')
    assert capture.value.task_id == '1' and capture.value.turn_id == 'turn-1'
    with pytest.raises(StoreError) as error:
        workflow.resume(CONTEXT, '1', 'Continue')
    assert error.value.code == 'TASK_BUSY'
    assert len(harness.calls) == 1


def test_harness_failure_is_captured_without_exposing_exception_values():
    workflow, store, _, _ = app(RuntimeError('private-token'))
    result = workflow.start(CONTEXT, 'Inspect cache')
    assert result.reply.error == 'HARNESS_UNAVAILABLE'
    assert 'private-token' not in result.reply.text
    assert store.turns[result.task_id][2] == result.reply


@pytest.mark.parametrize('prompt', ['', '  ', None, 4, '\ud800'])
def test_invalid_request_has_no_storage_or_external_side_effect(prompt):
    workflow, store, harness, _ = app()
    with pytest.raises(StoreError) as error:
        workflow.start(CONTEXT, prompt)
    assert error.value.code == 'INVALID_INPUT'
    assert not store.tasks and not harness.calls


def test_empty_followup_is_rejected_before_claiming_or_invoking():
    workflow, store, harness, _ = app(HarnessReply('Saved', 'end_turn'))
    first = workflow.start(CONTEXT, 'Inspect cache')
    with pytest.raises(StoreError) as error:
        workflow.resume(CONTEXT, first.task_id)
    assert error.value.code == 'INVALID_INPUT'
    assert not store.busy and len(harness.calls) == 1


def test_project_switching_lists_only_trusted_contexts_and_creates_separate_tasks():
    workflow, _, harness, _ = app(HarnessReply('One', 'end_turn'), HarnessReply('Two', 'end_turn'))
    other = replace(CONTEXT, project_id='analytics')
    assert workflow.list_projects([CONTEXT, other]) == [
        {'id': 'charging', 'name': 'Charging'}, {'id': 'analytics', 'name': 'Analytics'}]
    first, second = workflow.start(CONTEXT, 'First'), workflow.start(other, 'Second')
    assert first.task_id != second.task_id
    assert harness.calls[1][0].harness_arn == 'harness-analytics'


@pytest.mark.parametrize('contexts', [[CONTEXT, CONTEXT],
    [CONTEXT, replace(CONTEXT, actor_id='bob', project_id='analytics')], None])
def test_mixed_or_duplicate_project_contexts_are_rejected(contexts):
    workflow, _, _, _ = app()
    with pytest.raises(StoreError) as error:
        workflow.list_projects(contexts)
    assert error.value.code == 'INVALID_INPUT'
