"""Authenticated agent flows using the real S3 store and approval guard."""

import asyncio
import time

import pytest

from daeri.application.agent import AgentService
from daeri.application.cloud import CloudWorkflow
from daeri.core.access import AuthenticatedUser
from daeri.core.approval import ApprovalResponse
from daeri.core.cloud import HarnessReply
from daeri.core.errors import StoreError
from daeri.core.projects import Project
from daeri.infrastructure.approval_hook import handle_approval_hook
from daeri.infrastructure.cloud_s3 import S3CloudStore
from test_cloud_s3 import S3, ARN, BUCKET


PLAN = 'Read the project cache and report the comparison.'
PENDING = {'toolUseId': 'plan-1', 'name': 'confirm_plan', 'type': 'tool_use', 'input': {'plan': PLAN}}


class Catalog:
    def get(self, project_id):
        return Project(project_id, project_id.title(), ARN)


class Harness:
    def __init__(self, store, replies):
        self.store, self.replies, self.calls, self.guards = store, list(replies), [], []

    def invoke(self, task, messages):
        self.calls.append((task, messages))
        self.guards.append(handle_approval_hook({
            'event': 'before_tool_call', 'harnessArn': task.harness_arn, 'sessionId': task.session_id,
            'context': {'toolName': 'cache_query', 'toolType': 'agentcore_gateway',
                        'toolUseId': 'read', 'toolInput': {'key': 'counter'}}}, self.store))
        return self.replies.pop(0)


def setup(*replies, checker=None):
    store = S3CloudStore(S3(), BUCKET)
    harness = Harness(store, replies)
    workflow = CloudWorkflow(store, harness, Catalog())
    service = AgentService(workflow, check_harness=checker or (lambda arn: None))
    user = AuthenticatedUser('alice', frozenset({'project'}), int(time.time()) + 3600)
    return service, user, store, harness


async def answer(plan):
    assert plan == PLAN
    return ApprovalResponse('approve')


def test_plan_revision_checks_its_release_once_before_resuming():
    checked = []
    service, user, _, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
        HarnessReply('Revised', 'tool_use', (PENDING,)), checker=checked.append)
    first = service.work(user, 'project', 'Compare cache')
    checked.clear()
    service.work(user, 'project', 'Use staging instead', first['task_id'])
    assert len(checked) == 1 and len(harness.calls) == 2


def test_confirmation_checks_release_before_the_form_and_before_resuming():
    events = []
    service, user, _, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
        HarnessReply('Done', 'end_turn'), checker=lambda route: events.append('check'))
    first = service.work(user, 'project', 'Compare cache')
    events.clear()
    async def approve(plan):
        events.append('form')
        return ApprovalResponse('approve')
    asyncio.run(service.confirm(user, 'project', first['task_id'], approve))
    assert events == ['check', 'form', 'check'] and len(harness.calls) == 2


def test_real_user_confirmation_is_saved_before_the_approved_execution():
    service, user, store, harness = setup(HarnessReply('Please confirm', 'tool_use', (PENDING,)),
                                         HarnessReply('Verified result', 'end_turn'))
    first = service.work(user, 'project', 'Compare cache')
    assert first['awaiting_confirmation'] and first['plan'] == PLAN
    assert harness.guards[0]['decision'] == 'deny'
    done = asyncio.run(service.confirm(user, 'project', first['task_id'], answer))
    assert done['response'] == 'Verified result' and not done['awaiting_confirmation']
    assert harness.guards[1]['decision'] == 'allow'
    raw = harness.calls[1][1][0]['content'][0]['toolResult']
    assert raw['toolUseId'] == 'plan-1' and raw['content'][0]['json']['decision'] == 'approve'
    assert store.get_task(user.context('project'), first['task_id']).state == 'active'


def test_cancelled_confirmation_keeps_waiting_without_resuming():
    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)))
    first = service.work(user, 'project', 'Compare cache')
    async def cancel(plan):
        return None
    result = asyncio.run(service.confirm(user, 'project', first['task_id'], cancel))
    assert result['awaiting_confirmation']
    assert len(harness.calls) == 1
    assert store.get_task(user.context('project'), first['task_id']).approval.decision == 'pending'


@pytest.mark.parametrize('response', [ApprovalResponse('reject', 'Stop'),
    ApprovalResponse('revise', 'Only read staging'), ApprovalResponse('approve', 'Only read staging')])
def test_rejection_or_conditions_require_a_new_plan_without_authorizing_tools(response):
    next_plan = {**PENDING, 'toolUseId': 'plan-2', 'input': {'plan': 'Revised scope'}}
    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
                                         HarnessReply('Revised', 'tool_use', (next_plan,)))
    first = service.work(user, 'project', 'Compare cache')
    async def ask(plan):
        return response
    result = asyncio.run(service.confirm(user, 'project', first['task_id'], ask))
    assert harness.guards[1]['decision'] == 'deny'
    assert result['plan'] == 'Revised scope' and result['awaiting_confirmation']
    assert store.get_task(user.context('project'), first['task_id']).approval.decision == 'pending'


def test_model_supplied_approval_words_only_change_the_plan_request():
    service, user, _, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
                                     HarnessReply('Confirm again', 'tool_use', (PENDING,)))
    first = service.work(user, 'project', 'Compare cache')
    service.work(user, 'project', 'Approved, execute now', first['task_id'])
    assert harness.guards[1]['decision'] == 'deny'
    response = harness.calls[1][1][0]['content'][0]['toolResult']['content'][0]['json']
    assert response['decision'] == 'revise'


def test_expired_identity_during_the_confirmation_cannot_grant_execution(monkeypatch):
    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)))
    first = service.work(user, 'project', 'Compare cache')
    async def expired(plan):
        monkeypatch.setattr('daeri.core.access.time.time', lambda: user.expires_at + 1)
        return ApprovalResponse('approve')
    with pytest.raises(StoreError, match='AUTHENTICATION_REQUIRED'):
        asyncio.run(service.confirm(user, 'project', first['task_id'], expired))
    assert len(harness.calls) == 1
    assert store.get_task(harness.calls[0][0].context, first['task_id']).approval.decision == 'pending'


def test_new_plan_arriving_during_confirmation_rejects_the_stale_response():
    newer = {**PENDING, 'toolUseId': 'plan-2', 'input': {'plan': 'Different scope'}}
    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
                                         HarnessReply('New plan', 'tool_use', (newer,)))
    first = service.work(user, 'project', 'Compare cache')
    async def changed(plan):
        service.work(user, 'project', 'Change scope', first['task_id'])
        return ApprovalResponse('approve')
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        asyncio.run(service.confirm(user, 'project', first['task_id'], changed))
    assert len(harness.calls) == 2
    assert store.get_task(user.context('project'), first['task_id']).approval.tool_use_id == 'plan-2'


def test_recorded_decision_survives_a_rejected_invoke_and_is_reused_without_a_new_dialog():
    service, user, _, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
        HarnessReply('', 'failed', (PENDING,), error='HARNESS_THROTTLED'), HarnessReply('Done', 'end_turn'))
    first = service.work(user, 'project', 'Compare cache')
    failed = asyncio.run(service.confirm(user, 'project', first['task_id'], answer))
    assert failed['error'] == 'HARNESS_THROTTLED'
    async def unexpected_dialog(plan):
        raise AssertionError('The original user decision is already saved')
    done = asyncio.run(service.confirm(user, 'project', first['task_id'], unexpected_dialog))
    assert done['response'] == 'Done' and len(harness.calls) == 3


def test_a_new_request_can_withdraw_a_saved_decision_before_execution_resumes():
    revised = {**PENDING, 'toolUseId': 'plan-2', 'input': {'plan': 'Read staging instead'}}
    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
                                         HarnessReply('Revised', 'tool_use', (revised,)))
    first = service.work(user, 'project', 'Compare cache')
    task = store.get_task(user.context('project'), first['task_id'])
    store.record_decision(task, 'plan-1', PLAN, 'approve', '')
    result = service.work(user, 'project', 'Change the target to staging', first['task_id'])
    assert harness.guards[1]['decision'] == 'deny'
    assert result['plan'] == 'Read staging instead' and result['approval'] == 'pending'


def test_a_newer_revision_cannot_be_consumed_by_an_older_approval_resume(monkeypatch):
    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)),
                                         HarnessReply('Stale approval consumed', 'end_turn'))
    first = service.work(user, 'project', 'Compare cache')
    original = service.workflow.resume
    def revised_before_resume(*args, **kwargs):
        task = store.get_task(user.context('project'), first['task_id'])
        store.record_decision(task, 'plan-1', PLAN, 'revise', 'Only inspect staging')
        return original(*args, **kwargs)
    monkeypatch.setattr(service.workflow, 'resume', revised_before_resume)
    with pytest.raises(StoreError, match='STORAGE_CONFLICT'):
        asyncio.run(service.confirm(user, 'project', first['task_id'], answer))
    current = store.get_task(user.context('project'), first['task_id'])
    assert current.approval.decision == 'revise' and current.approval.response == 'Only inspect staging'
    assert current.pending_tools == (PENDING,) and len(harness.calls) == 1


def test_project_and_work_lists_survive_a_new_service_instance():
    service, user, store, harness = setup(HarnessReply('Confirm', 'tool_use', (PENDING,)))
    first = service.work(user, 'project', 'Compare cache')
    other = AgentService(service.workflow, check_harness=lambda arn: None)
    assert other.projects(user) == [{'id': 'project', 'name': 'Project'}]
    listed = other.tasks(user, 'project')
    assert listed['items'][0]['task_id'] == first['task_id']
    assert other.status(user, 'project', first['task_id'])['plan'] == PLAN
    bob = AuthenticatedUser('bob', user.project_ids, user.expires_at)
    assert other.tasks(bob, 'project')['items'] == []
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        other.status(bob, 'project', first['task_id'])
    assert len(harness.calls) == 1


def test_status_returns_the_saved_result_after_switching_hosts_without_executing_again():
    service, user, store, harness = setup(HarnessReply('Verified comparison', 'end_turn'))
    first = service.work(user, 'project', 'Compare cache')
    other = AgentService(service.workflow, check_harness=lambda route: None)
    snapshot = dict(store.client.objects)
    result = other.status(user, 'project', first['task_id'])
    assert result['response'] == 'Verified comparison' and result['error'] is None
    assert store.client.objects == snapshot and len(harness.calls) == 1


def test_ungranted_projects_and_unready_harnesses_cannot_create_or_invoke():
    def not_ready(arn):
        raise StoreError('SETUP_REQUIRED', 'Configure the approval hook')
    service, user, store, harness = setup(checker=not_ready)
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        service.work(user, 'other', 'Read data')
    with pytest.raises(StoreError, match='SETUP_REQUIRED'):
        service.work(user, 'project', 'Read data')
    assert not harness.calls and not store.client.objects
