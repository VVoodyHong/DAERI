"""Deployment inputs for the DAERI MCP Runtime; secrets stay in AWS providers."""

from typing import Any
import re

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

    @field_validator('network_configuration')
    @classmethod
    def validate_network(cls, value):
        from botocore.exceptions import ParamValidationError
        from botocore.loaders import create_loader
        from botocore.model import ServiceModel
        from botocore.validate import validate_parameters

        model = ServiceModel(create_loader().load_service_model('bedrock-agentcore-control', 'service-2'))
        shape = model.shape_for('NetworkConfiguration')
        try:
            validate_parameters(value, shape)
        except ParamValidationError:
            raise ValueError('Supply a native PUBLIC or VPC network configuration.') from None
        mode = value['networkMode']
        if mode not in {'PUBLIC', 'VPC'} or (mode == 'VPC') != ('networkModeConfig' in value):
            raise ValueError('Supply a native PUBLIC or VPC network configuration.')
        for field, items in value.get('networkModeConfig', {}).items():
            if field in {'securityGroups', 'subnets'}:
                pattern = shape.members['networkModeConfig'].members[field].member.metadata['pattern']
                if any(not re.fullmatch(pattern, item) for item in items):
                    raise ValueError('Supply valid VPC subnet and security group identifiers.')
        return value
