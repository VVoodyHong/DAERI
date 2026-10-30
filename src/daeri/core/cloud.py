"""Deployment-independent contracts for the shared cloud service."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AccessContext:
    """Current identity and grants supplied by the authenticated server boundary."""

    actor_id: str
    project_id: str
    service_id: str
    source_ids: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'source_ids', frozenset(self.source_ids))


@dataclass(frozen=True)
class CloudTask:
    id: str
    session_id: str
    context: AccessContext
    pending_tools: tuple[dict[str, Any], ...] = ()
    goal: str = ''


@dataclass(frozen=True)
class HarnessReply:
    text: str
    stop_reason: str
    pending_tools: tuple[dict[str, Any], ...] = ()
    observations: tuple[dict[str, Any], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class SharedMemory:
    id: str
    text: str
    source_ids: frozenset[str]


@dataclass(frozen=True)
class CloudTurn:
    task_id: str
    turn_id: str
    reply: HarnessReply
    recalled_ids: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeJob:
    id: str
    token: str
    context: AccessContext
    prompt: str
    response: str
    text: str | None = None


class CloudStore(Protocol):
    def create_task(self, context: AccessContext) -> CloudTask: ...
    def get_task(self, context: AccessContext, task_id: str) -> CloudTask: ...
    def begin_turn(self, task: CloudTask) -> str: ...
    def finish_turn(self, task: CloudTask, turn_id: str, prompt: str,
                    reply: HarnessReply, recalled_ids: tuple[str, ...]) -> None: ...
    def search(self, context: AccessContext, query: str, vector: list[float] | None,
               model_id: str | None, limit: int) -> list[SharedMemory]: ...
    def claim_job(self) -> KnowledgeJob | None: ...
    def save_text(self, job: KnowledgeJob, text: str) -> None: ...
    def complete_job(self, job: KnowledgeJob, vector: list[float] | None,
                     model_id: str | None) -> None: ...
    def retry_job(self, job: KnowledgeJob, code: str) -> None: ...


class Harness(Protocol):
    def invoke(self, task: CloudTask, messages: list[dict[str, Any]]) -> HarnessReply: ...


class CloudEmbedder(Protocol):
    @property
    def model_id(self) -> str: ...
    def embed_query(self, text: str) -> list[float]: ...
    def embed_document(self, text: str) -> list[float]: ...


class ExperienceExtractor(Protocol):
    def extract(self, prompt: str, response: str) -> str: ...
