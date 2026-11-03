"""Trusted central project metadata, independent of storage and authentication."""

from dataclasses import dataclass
import re
from typing import Protocol


def valid_harness_release(endpoint, version) -> bool:
    return bool(isinstance(endpoint, str) and endpoint != 'DEFAULT'
                and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', endpoint)
                and isinstance(version, str) and re.fullmatch(r'[1-9][0-9]{0,4}', version))


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    harness_arn: str
    harness_endpoint: str = ''
    harness_version: str = ''


class ProjectCatalog(Protocol):
    def get(self, project_id: str) -> Project: ...
