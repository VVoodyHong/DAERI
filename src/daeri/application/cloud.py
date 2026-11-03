"""Project routing and durable harness turns; Memory owns extraction and retrieval."""

from copy import deepcopy
from typing import Any

from daeri.core.cloud import AccessContext, CloudStore, CloudTask, CloudTurn, Harness, HarnessReply
from daeri.core.errors import StoreError
from daeri.core.projects import Project, ProjectCatalog
from daeri.core.serialization import validate_storable_json


def _validate_context(context: AccessContext) -> None:
    if not isinstance(context, AccessContext) or not all(
        isinstance(value, str) and value.strip() for value in (context.actor_id, context.project_id)
    ):
        raise StoreError('INVALID_INPUT', 'Current identity and project membership are required')
    try:
        validate_storable_json((context.actor_id, context.project_id))
    except (TypeError, ValueError, RecursionError):
        raise StoreError('INVALID_INPUT', 'Current identity and project must be valid UTF-8.') from None


def _validate_prompt(prompt: str, *, required: bool = False) -> None:
    if not isinstance(prompt, str) or (required and not prompt.strip()):
        raise StoreError('INVALID_INPUT', 'A text request is required')
    try:
        validate_storable_json(prompt)
    except (TypeError, ValueError, RecursionError):
        raise StoreError('INVALID_INPUT', 'Request text must be valid UTF-8.') from None


def _tool_results(task: CloudTask, results: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    if not isinstance(results, (tuple, list)):
        raise StoreError('INVALID_INPUT', 'Tool results must be a list of actual pending responses')
    expected = {tool['toolUseId'] for tool in task.pending_tools}
    normalized = {}
    for result in results:
        if not isinstance(result, dict) or not set(result) <= {'toolUseId', 'status', 'content', 'type'}:
            raise StoreError('INVALID_INPUT', 'Tool result fields are invalid')
        tool_id = result.get('toolUseId')
        if not isinstance(tool_id, str) or tool_id not in expected or tool_id in normalized:
            raise StoreError('INVALID_INPUT', 'Tool results must match each pending call exactly once')
        if result.get('status') not in ('success', 'error') or result.get('type', 'tool_use') != 'tool_use':
            raise StoreError('INVALID_INPUT', 'Tool result status or type is invalid')
        content = result.get('content')
        if not isinstance(content, list) or any(
            not isinstance(block, dict) or len(block) != 1
            or not set(block) <= {'text', 'json'}
            or ('text' in block and not isinstance(block['text'], str))
            for block in content
        ):
            raise StoreError('INVALID_INPUT', 'Tool result content must contain text or JSON blocks')
        try:
            validate_storable_json(content)
            normalized[tool_id] = {'toolResult': {**deepcopy(result), 'type': 'tool_use'}}
        except (TypeError, ValueError, RecursionError):
            raise StoreError('INVALID_INPUT', 'Tool result content must be storable UTF-8 JSON.') from None
    if normalized.keys() != expected:
        raise StoreError('INVALID_INPUT', 'Actual responses for every pending tool call are required')
    return [normalized[tool['toolUseId']] for tool in task.pending_tools]


class CloudWorkflow:
    def __init__(self, store: CloudStore, harness: Harness, catalog: ProjectCatalog):
        self.store, self.harness, self.catalog = store, harness, catalog

    def list_projects(self, contexts: list[AccessContext] | tuple[AccessContext, ...]) -> list[dict[str, str]]:
        """Display projects whose complete shared scope the identity boundary authorized."""
        if not isinstance(contexts, (list, tuple)):
            raise StoreError('INVALID_INPUT', 'Supply the currently authorized project contexts')
        projects, actors = {}, set()
        for context in contexts:
            _validate_context(context)
            actors.add(context.actor_id)
            if len(actors) > 1 or context.project_id in projects:
                raise StoreError('INVALID_INPUT', 'Supply one current user and one context per project')
            project = self.catalog.get(context.project_id)
            projects[project.id] = {'id': project.id, 'name': project.name}
        return list(projects.values())

    def start(self, context: AccessContext, prompt: str, *, project: Project | None = None) -> CloudTurn:
        _validate_prompt(prompt, required=True)
        _validate_context(context)
        project = self.catalog.get(context.project_id) if project is None else project
        if not isinstance(project, Project) or project.id != context.project_id:
            raise StoreError('ACCESS_DENIED', 'The checked release belongs to another project.')
        task = self.store.create_task(context, project.harness_arn, prompt,
                                      harness_endpoint=project.harness_endpoint,
                                      harness_version=project.harness_version)
        return self._invoke(task, prompt, (), [])

    def resume(self, context: AccessContext, task_id: str, prompt: str = '',
               tool_results: tuple[dict[str, Any], ...] = (), *, expected_etag: str | None = None) -> CloudTurn:
        _validate_prompt(prompt)
        task = self.get_task(context, task_id)
        if expected_etag is not None and task.etag != expected_etag:
            raise StoreError('STORAGE_CONFLICT', 'The recorded plan response changed before execution could begin.')
        results = _tool_results(task, tool_results)
        if not prompt.strip() and not results:
            raise StoreError('INVALID_INPUT', 'Supply a follow-up request or actual pending tool responses')
        return self._invoke(task, prompt, tuple(deepcopy(tool_results)), results)

    def recover(self, context: AccessContext, task_id: str) -> CloudTurn:
        """Recover a recorded turn without repeating external execution."""
        self.get_task(context, task_id)
        return self.store.recover_turn(context, task_id)

    def get_task(self, context: AccessContext, task_id: str) -> CloudTask:
        _validate_context(context)
        self.catalog.get(context.project_id)
        task = self.store.get_task(context, task_id)
        if task.context != context:
            raise StoreError('ACCESS_DENIED', 'Task access does not match the current context')
        return task

    def _invoke(self, task: CloudTask, prompt: str, tool_results: tuple[dict[str, Any], ...],
                results: list[dict[str, Any]]) -> CloudTurn:
        content = ([{'text': prompt}] if prompt else []) + results
        try:
            turn_id = self.store.begin_turn(task, prompt, tool_results)
        except StoreError as error:
            error.task_id = task.id
            raise
        try:
            reply = self.harness.invoke(task, [{'role': 'user', 'content': content}])
        except Exception:
            reply = HarnessReply('The harness response could not be obtained. Check the saved execution state before retrying.',
                                 'error', error='HARNESS_UNAVAILABLE')
        try:
            self.store.finish_turn(task, turn_id, prompt, reply, tool_results)
        except StoreError as error:
            error.task_id, error.turn_id = task.id, turn_id
            raise
        return CloudTurn(task.id, turn_id, reply)
