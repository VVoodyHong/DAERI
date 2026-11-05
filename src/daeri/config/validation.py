"""Shared resource types for operator configuration boundaries."""

from functools import lru_cache
import re
from typing import Annotated

from botocore.exceptions import ParamValidationError
from botocore.loaders import create_loader
from botocore.model import ServiceModel
from botocore.validate import validate_parameters
from pydantic import StringConstraints


ApprovalHookArn = Annotated[str, StringConstraints(pattern=(
    r'^arn:[a-z0-9-]+:lambda:[a-z0-9-]+:[0-9]{12}:function:'
    r'[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_-]+|:\$LATEST)?$'))]


@lru_cache(maxsize=1)
def control_model():
    """Load service shapes without reading AWS profiles or constructing clients."""
    return ServiceModel(create_loader().load_service_model('bedrock-agentcore-control', 'service-2'))


def validate_native(value, shape):
    validate_parameters(value, shape)
    measured = len(value) if shape.type_name in {'string', 'list', 'map'} else value
    if shape.type_name in {'string', 'list', 'map', 'integer', 'long', 'float', 'double'}:
        if (('min' in shape.metadata and measured < shape.metadata['min'])
                or ('max' in shape.metadata and measured > shape.metadata['max'])):
            raise ValueError('Supply values within native service limits.')
    if shape.type_name == 'structure':
        if shape.metadata.get('union') and len(value) != 1:
            raise ValueError('Supply one native configuration variant.')
        for name, item in value.items():
            validate_native(item, shape.members[name])
    elif shape.type_name in {'list', 'map'}:
        if shape.type_name == 'map':
            for key in value:
                validate_native(key, shape.key)
        for item in value if shape.type_name == 'list' else value.values():
            validate_native(item, shape.member if shape.type_name == 'list' else shape.value)
    elif shape.type_name == 'string':
        if 'enum' in shape.metadata and value not in shape.metadata['enum']:
            raise ValueError('Supply a supported native configuration variant.')
        if 'pattern' in shape.metadata and not re.fullmatch(shape.metadata['pattern'], value):
            raise ValueError('Supply a valid native resource reference.')


def validate_network_configuration(value):
    """Validate native Runtime networking without creating an AWS client."""
    try:
        validate_native(value, control_model().shape_for('NetworkConfiguration'))
    except (ParamValidationError, ValueError):
        raise ValueError('Supply a native PUBLIC or VPC network configuration.') from None
    mode = value['networkMode']
    if (mode == 'VPC') != ('networkModeConfig' in value):
        raise ValueError('Supply a native PUBLIC or VPC network configuration.')
    if set(value.get('networkModeConfig', {})) - {'subnets', 'securityGroups'}:
        raise ValueError('Supported VPC deployments accept only subnets and securityGroups.')
    return value
