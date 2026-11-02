"""Trusted central project metadata, independent of storage and authentication."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    harness_arn: str


class ProjectCatalog(Protocol):
    def get(self, project_id: str) -> Project: ...
