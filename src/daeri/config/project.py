"""Operator input for publishing one project with managed shared memory."""

from hashlib import sha256
from typing import Annotated

from pydantic import StringConstraints, ValidationError, field_validator

from daeri.config.cloud import _Settings
from daeri.config.yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.infrastructure.project_catalog import _HARNESS_ARN, normalize_project_prefix


class ProjectSettings(_Settings):
    region: Annotated[str, StringConstraints(pattern=r'^[a-z]{2}(?:-[a-z]+)+-[0-9]+$')] | None = None
    project_parameter_prefix: str = '/daeri/projects/'
    project_id: Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')]
    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    harness_arn: str
    approval_hook_arn: str
    memory_id: Annotated[str, StringConstraints(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,99}-[A-Za-z0-9]{10}$')] | None = None
    memory_execution_role_arn: str | None = None
    encryption_key_arn: str | None = None

    @field_validator('project_parameter_prefix')
    @classmethod
    def project_prefix(cls, value):
        return normalize_project_prefix(value)

    @field_validator('name')
    @classmethod
    def display_name(cls, value):
        if value.strip() != value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Supply a project display name.')
        return value

    @field_validator('harness_arn')
    @classmethod
    def harness_route(cls, value):
        if not _HARNESS_ARN.fullmatch(value):
            raise ValueError('Supply an existing managed harness ARN.')
        return value

    @field_validator('approval_hook_arn', 'memory_execution_role_arn', 'encryption_key_arn')
    @classmethod
    def resource_arn(cls, value):
        if value is not None and (not value.startswith('arn:') or any(char.isspace() for char in value)):
            raise ValueError('Supply a resource ARN.')
        return value

    @property
    def memory_name(self):
        if self.memory_id:
            return self.memory_id.rsplit('-', 1)[0]
        # The catalogue and project identify a separately owned, stable Memory resource.
        key = self.project_parameter_prefix + self.project_id
        return 'Daeri_' + sha256(key.encode()).hexdigest()[:24]

    @classmethod
    def from_yaml(cls, path):
        try:
            return cls.model_validate(read_yaml(path))
        except ValidationError:
            raise StoreError('INVALID_INPUT', 'Project deployment settings are invalid.') from None
