"""Deployment-independent contracts for shared project access and durable work."""

from dataclasses import dataclass
from typing import Any, Protocol

from daeri.core.approval import PlanApproval


@dataclass(frozen=True)
class AccessContext:
    """Identity and full project membership supplied by the authenticated boundary."""

    actor_id: str
    project_id: str


@dataclass(frozen=True)
class CloudTask:
    id: str
    session_id: str
    context: AccessContext
    pending_tools: tuple[dict[str, Any], ...] = ()
    goal: str = ''
    harness_arn: str = ''
    etag: str = ''
    approval: PlanApproval | None = None
    state: str = 'active'
    harness_endpoint: str = ''
    harness_version: str = ''


@dataclass(frozen=True)
class HarnessReply:
    text: str
    stop_reason: str
    pending_tools: tuple[dict[str, Any], ...] = ()
    observations: tuple[dict[str, Any], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class CloudTurn:
    task_id: str
    turn_id: str
    reply: HarnessReply


class CloudStore(Protocol):
    def create_task(self, context: AccessContext, harness_arn: str, goal: str, *,
                    harness_endpoint: str = '', harness_version: str = '') -> CloudTask: ...
    def get_task(self, context: AccessContext, task_id: str) -> CloudTask: ...
    def begin_turn(self, task: CloudTask, prompt: str, tool_results: tuple[dict[str, Any], ...]) -> str: ...
    def finish_turn(self, task: CloudTask, turn_id: str, prompt: str, reply: HarnessReply,
                    tool_results: tuple[dict[str, Any], ...]) -> None: ...
    def recover_turn(self, context: AccessContext, task_id: str) -> CloudTurn: ...
    def read_turn(self, context: AccessContext, task_id: str) -> CloudTurn | None: ...
    def list_tasks(self, context: AccessContext, cursor: str | None = None,
                   limit: int = 20) -> dict: ...
    def record_decision(self, task: CloudTask, tool_use_id: str, plan: str,
                        decision: str, response: str) -> CloudTask: ...


class Harness(Protocol):
    def invoke(self, task: CloudTask, messages: list[dict[str, Any]]) -> HarnessReply: ...
