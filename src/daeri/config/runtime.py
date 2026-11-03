"""Deployment inputs for the DAERI MCP Runtime; secrets stay in AWS providers."""

from typing import Any

from pydantic import Field, field_validator

from daeri.config.server import ServiceSettings
from daeri.config.cloud import _Settings


class RuntimeSettings(_Settings):
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,47}$')
    image_uri: str = Field(min_length=1)
    role_arn: str = Field(pattern=r'^arn:[a-z0-9-]+:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$')
    server: ServiceSettings
    client_ids: list[str] = Field(min_length=1)
    runtime_id: str | None = Field(default=None, pattern=r'^[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]+$')
    endpoint: str = Field(default='PROD', pattern=r'^[A-Za-z][A-Za-z0-9_]{0,47}$')
    network_configuration: dict[str, Any] = Field(default_factory=lambda: {'networkMode': 'PUBLIC'})

    @field_validator('image_uri')
    @classmethod
    def validate_image(cls, value):
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError('Supply a container image URI.')
        return value

    @field_validator('client_ids')
    @classmethod
    def validate_clients(cls, value):
        if len(set(value)) != len(value) or any(not item or any(char.isspace() for char in item) for item in value):
            raise ValueError('Supply distinct public OAuth client IDs.')
        return value
