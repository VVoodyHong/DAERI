"""Immutable caller identity and project grants from a verified access token."""

from dataclasses import dataclass
import re
import time

from daeri.core.cloud import AccessContext
from daeri.core.errors import StoreError
from daeri.core.serialization import validate_storable_json


_PROJECT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')


@dataclass(frozen=True)
class AuthenticatedUser:
    actor_id: str
    project_ids: frozenset[str]
    expires_at: int
    managed_project_ids: frozenset[str] = frozenset()

    def __post_init__(self):
        if (not isinstance(self.actor_id, str) or not self.actor_id.strip()
                or not isinstance(self.project_ids, frozenset)
                or any(not isinstance(item, str) or not _PROJECT_ID.fullmatch(item) for item in self.project_ids)
                or not isinstance(self.managed_project_ids, frozenset)
                or not self.managed_project_ids.issubset(self.project_ids)
                or type(self.expires_at) is not int):
            raise StoreError('INVALID_INPUT', 'A verified identity, project grants and expiry are required.')
        try:
            validate_storable_json(self.actor_id)
        except (ValueError, TypeError):
            raise StoreError('INVALID_INPUT', 'The verified identity must be valid UTF-8.') from None

    def _current(self):
        if self.expires_at <= time.time():
            raise StoreError('AUTHENTICATION_REQUIRED', 'Sign in again before accessing the project.')

    def context(self, project_id: str) -> AccessContext:
        self._current()
        if not isinstance(project_id, str) or project_id not in self.project_ids:
            raise StoreError('ACCESS_DENIED', 'The current user cannot access this project.')
        return AccessContext(self.actor_id, project_id)

    def contexts(self) -> tuple[AccessContext, ...]:
        self._current()
        return tuple(AccessContext(self.actor_id, project) for project in sorted(self.project_ids))

    def manage(self, project_id: str) -> AccessContext:
        context = self.context(project_id)
        if project_id not in self.managed_project_ids:
            raise StoreError('ACCESS_DENIED', 'Only a project manager can change its shared connections.')
        return context
