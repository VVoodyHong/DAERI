"""Authenticated project work and host-confirmed approvals."""

import asyncio

from daeri.application.cloud import _validate_prompt
from daeri.core.approval import ApprovalResponse
from daeri.core.errors import StoreError


def _pending_plan(task):
    if task.state == 'running':
        raise StoreError('TASK_BUSY', 'The task already has an execution in progress.')
    approval = task.approval
    if (len(task.pending_tools) != 1 or task.pending_tools[0].get('name') != 'confirm_plan'
            or approval is None
            or task.pending_tools[0].get('toolUseId') != approval.tool_use_id
            or task.pending_tools[0].get('input') != {'plan': approval.plan}):
        raise StoreError('PLAN_UNAVAILABLE', 'There is no current plan awaiting confirmation.')
    return approval


def _view(task):
    return {'task_id': task.id, 'project_id': task.context.project_id, 'goal': task.goal,
            'state': task.state, 'plan': task.approval.plan if task.approval else None,
            'approval': task.approval.decision if task.approval else None,
            'awaiting_confirmation': bool(task.pending_tools) and task.state != 'running'}


class AgentService:
    def __init__(self, workflow, *, check_harness, artifacts=None):
        self.workflow, self.check_harness = workflow, check_harness
        self.artifacts = artifacts

    def projects(self, user):
        return self.workflow.list_projects(user.contexts())

    def tasks(self, user, project_id, cursor=None, limit=20):
        context = user.context(project_id)
        self.workflow.catalog.get(project_id)
        result = self.workflow.store.list_tasks(context, cursor, limit)
        return {**result, 'items': [_view(task) for task in result['items']]}

    def status(self, user, project_id, task_id):
        context = user.context(project_id)
        task = self.workflow.get_task(context, task_id)
        turn = self.workflow.store.read_turn(context, task_id)
        return {**_view(task), 'response': turn.reply.text if turn else None,
                'error': turn.reply.error if turn else None}

    def work(self, user, project_id, request, task_id=None):
        _validate_prompt(request, required=True)
        context = user.context(project_id)
        if task_id is None:
            project = self.workflow.catalog.get(project_id)
            self.check_harness(project)
            context = user.context(project_id)
            turn = self.workflow.start(context, request, project=project)
        else:
            task = self.workflow.get_task(context, task_id)
            if task.pending_tools:
                turn = self._decide(user, task, 'revise', request)
            else:
                self.check_harness(task)
                context = user.context(project_id)
                turn = self.workflow.resume(context, task_id, request)
        return self._result(user, project_id, turn)

    def recover(self, user, project_id, task_id):
        turn = self.workflow.recover(user.context(project_id), task_id)
        return self._result(user, project_id, turn)

    async def restart(self, user, project_id, task_id, ask):
        task = await asyncio.to_thread(self.workflow.get_task, user.context(project_id), task_id)
        review = await ask(task.goal, task.state)
        user.context(project_id)
        if review is None:
            return await asyncio.to_thread(self.status, user, project_id, task_id)
        _validate_prompt(review, required=True)
        await asyncio.to_thread(self.check_harness, task)
        turn = await asyncio.to_thread(self.workflow.restart, user.context(project_id), task_id,
                                       review, expected_etag=task.etag)
        return await asyncio.to_thread(self._result, user, project_id, turn)

    def files(self, user, project_id, task_id, cursor=None, limit=20):
        task = self.workflow.get_task(user.context(project_id), task_id)
        return self.artifacts.list(user, task, cursor, limit)

    def download(self, user, project_id, task_id, artifact_id):
        task = self.workflow.get_task(user.context(project_id), task_id)
        return self.artifacts.download(user, task, artifact_id)

    async def confirm(self, user, project_id, task_id, ask):
        task = await asyncio.to_thread(self.workflow.get_task, user.context(project_id), task_id)
        approval = _pending_plan(task)
        await asyncio.to_thread(self.check_harness, task)
        if approval.decision == 'pending':
            answer = await ask(approval.plan)
            user.context(project_id)
            if answer is None:
                return await asyncio.to_thread(self.status, user, project_id, task_id)
            if not isinstance(answer, ApprovalResponse) or answer.decision not in {'approve', 'revise', 'reject'}:
                raise StoreError('INVALID_INPUT', 'Choose approval, revision or rejection in the confirmation form.')
            _validate_prompt(answer.response)
            decision = 'revise' if answer.decision == 'approve' and answer.response.strip() else answer.decision
            if decision == 'revise' and not answer.response.strip():
                raise StoreError('INVALID_INPUT', 'Describe the requested revision before resubmitting the plan.')
            turn = await asyncio.to_thread(self._decide, user, task, decision, answer.response)
        else:
            # A recorded response can survive a request rejected before the harness accepted it.
            turn = await asyncio.to_thread(self._resume_decision, user, task)
        return await asyncio.to_thread(self._result, user, project_id, turn)

    def _decide(self, user, task, decision, response):
        user.context(task.context.project_id)
        approval = _pending_plan(task)
        task = self.workflow.store.record_decision(task, approval.tool_use_id, approval.plan, decision, response)
        return self._resume_decision(user, task)

    def _resume_decision(self, user, task):
        context = user.context(task.context.project_id)
        approval = _pending_plan(task)
        if approval.decision not in {'approve', 'revise', 'reject'}:
            raise StoreError('PLAN_UNAVAILABLE', 'A host-confirmed response is required to continue.')
        self.check_harness(task)
        context = user.context(task.context.project_id)
        result = {'toolUseId': approval.tool_use_id, 'status': 'success',
                  'content': [{'json': {'decision': approval.decision, 'response': approval.response}}]}
        return self.workflow.resume(context, task.id, tool_results=(result,), expected_etag=task.etag)

    def _result(self, user, project_id, turn):
        task = self.workflow.get_task(user.context(project_id), turn.task_id)
        return {**_view(task), 'response': turn.reply.text, 'error': turn.reply.error}
