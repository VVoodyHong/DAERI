"""Operator input for publishing one project with managed shared memory."""

from typing import Annotated

from pydantic import StringConstraints, ValidationError, field_validator

from daeri.config.cloud import _Settings
from daeri.config.validation import ApprovalHookArn
from daeri.config.yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.core.projects import PROJECT_KEY_PATTERN, valid_harness_arn
from daeri.infrastructure.project_catalog import normalize_project_prefix


class ProjectSettings(_Settings):
    region: Annotated[str, StringConstraints(pattern=r'^[a-z]{2}(?:-[a-z]+)+-[0-9]+$')] | None = None
    project_parameter_prefix: str = '/daeri/projects/'
    project_id: Annotated[str, StringConstraints(pattern=PROJECT_KEY_PATTERN)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    harness_arn: str
    harness_version: Annotated[str, StringConstraints(pattern=r'^[1-9][0-9]{0,4}$')]
    approval_hook_arn: ApprovalHookArn
    memory_id: Annotated[str, StringConstraints(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,99}-[A-Za-z0-9]{10}$')]

    @field_validator('project_parameter_prefix')
    @classmethod
    def project_prefix(cls, value):
        try:
            return normalize_project_prefix(value)
        except StoreError as error:
            raise ValueError(error.message) from None

    @field_validator('name')
    @classmethod
    def display_name(cls, value):
        if value.strip() != value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Supply a project display name.')
        return value

    @field_validator('harness_arn')
    @classmethod
    def harness_route(cls, value):
        if not valid_harness_arn(value):
            raise ValueError('Supply an existing managed harness ARN.')
        return value

    @classmethod
    def from_yaml(cls, path):
        try:
            return cls.model_validate(read_yaml(path))
        except ValidationError:
            raise StoreError('INVALID_INPUT', 'Project deployment settings are invalid.') from None
