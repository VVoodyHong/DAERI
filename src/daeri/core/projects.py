"""Trusted central project metadata, independent of storage and authentication."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    harness_arn: str
    harness_endpoint: str = ''
    harness_version: str = ''


class ProjectCatalog(Protocol):
    def get(self, project_id: str) -> Project: ...
