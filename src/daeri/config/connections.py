"""Trusted operator connector templates and project-owned connection bindings."""

import re
from string import Formatter
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, quote, urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from daeri.config.cloud import _Settings
from daeri.config.validation import control_model, validate_native, validate_network_configuration
from daeri.core.errors import StoreError
from daeri.core.projects import PROJECT_KEY_PATTERN
from daeri.infrastructure.project_catalog import normalize_project_prefix


ConnectionId = Annotated[str, StringConstraints(pattern=PROJECT_KEY_PATTERN)]
_SECRET_ARN = re.compile(r'arn:(aws|aws-cn|aws-us-gov):secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+')
_ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_SENSITIVE = re.compile(r'(password|passwd|secret|token|api_?key|private_?key)', re.I)


def https_url(value):
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('Supply a HTTPS endpoint without credentials.')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise ValueError('Supply a HTTPS endpoint without credentials.')
    parsed.port
    if any(_SENSITIVE.search(key) or key.lower() in {'auth', 'authorization', 'key', 'signature', 'sig', 'credential'}
           for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError('Use a trusted credential provider instead of URL credentials.')
    return value


def render_url(template, inputs):
    fields = []
    for _, key, spec, conversion in Formatter().parse(template):
        if key is not None:
            if key not in inputs or spec or conversion:
                raise ValueError('URL substitutions must name declared inputs.')
            fields.append(key)
    if len(fields) == 1 and template == '{' + fields[0] + '}':
        return https_url(inputs[fields[0]])
    return https_url(template.format(**{key: quote(str(value), safe='') for key, value in inputs.items()}))


def validate_url_template(template, inputs):
    keys = [key for _, key, _, _ in Formatter().parse(template) if key is not None]
    full = len(keys) == 1 and template == '{' + keys[0] + '}'
    render_url(template, {key: ('https://example.com' if full else 'example') for key in inputs})
    return keys


class ConnectionSettings(_Settings):
    prefix: str = '/daeri/connections/'

    @field_validator('prefix')
    @classmethod
    def normalized_prefix(cls, value):
        try:
            return normalize_project_prefix(value)
        except StoreError:
            raise ValueError('Supply an absolute connection parameter prefix.') from None


class ConnectorField(_Settings):
    title: str = Field(min_length=1, max_length=256)
    type: Literal['string', 'integer', 'boolean'] = 'string'
    required: bool = True
    default: str | int | bool | None = None
    env: str | None = Field(default=None, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    secret_ref: bool = False

    @model_validator(mode='after')
    def field_contract(self):
        if self.secret_ref and (self.type != 'string' or 'default' in self.model_fields_set):
            raise ValueError('Secret fields require a string reference without a default.')
        expected = {'string': str, 'integer': int, 'boolean': bool}[self.type]
        if 'default' in self.model_fields_set and type(self.default) is not expected:
            raise ValueError('A field default must match its declared type.')
        return self


class ConnectorTemplate(_Settings):
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default='', max_length=1024)
    kind: Literal['runtime', 'mcp', 'openapi']
    inputs: dict[ConnectionId, ConnectorField] = Field(default_factory=dict)
    image_uri: str | None = None
    command: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    endpoint: str | None = None
    openapi_schema: dict[str, Any] | None = None

    @model_validator(mode='after')
    def target_contract(self):
        if 'credential_id' in self.inputs:
            raise ValueError('credential_id is reserved for a project credential alias.')
        for key, field in self.inputs.items():
            if _SENSITIVE.search(key) and not field.secret_ref:
                raise ValueError('Credential inputs must use secret references.')
        if self.kind == 'runtime':
            if (not self.image_uri or not self.command or self.endpoint is not None or self.openapi_schema is not None
                    or any(char.isspace() or ord(char) < 32 for char in self.image_uri)
                    or any(not item or '\x00' in item for item in self.command)):
                raise ValueError('Supply the packaged image and argument vector for a Runtime connector.')
            environments = [field.env for field in self.inputs.values()]
            if ('DAERI_CONNECTOR_CONFIG' in self.environment or 'DAERI_CONNECTOR_CONFIG' in environments
                    or None in environments or len(environments) != len(set(environments))
                    or set(environments).intersection(self.environment)
                    or any(not _ENV.fullmatch(key) or _SENSITIVE.search(key) or '\x00' in value
                           for key, value in self.environment.items())):
                raise ValueError('Supply distinct runtime environment names and nonsecret static values.')
        else:
            if self.image_uri is not None or self.command or self.environment or any(field.secret_ref for field in self.inputs.values()):
                raise ValueError('Remote targets use project credential providers.')
            if self.kind == 'mcp':
                if self.endpoint is None or self.openapi_schema is not None:
                    raise ValueError('Supply a remote MCP endpoint template.')
                validate_url_template(self.endpoint, self.inputs)
            elif self.openapi_schema is None or self.endpoint is not None:
                raise ValueError('Supply an inline OpenAPI schema for an API connector.')
            else:
                servers = self.openapi_schema.get('servers', [])
                if not isinstance(servers, list) or not servers:
                    raise ValueError('Supply HTTPS OpenAPI server URLs.')
                used = set()
                for server in servers:
                    if not isinstance(server, dict) or not isinstance(server.get('url'), str):
                        raise ValueError('Supply HTTPS OpenAPI server URLs.')
                    used.update(validate_url_template(server['url'], self.inputs))
                if set(self.inputs) - used:
                    raise ValueError('OpenAPI setup inputs must configure a server URL.')

        return self


class ProjectConnections(_Settings):
    gateway_arn: str = Field(pattern=r'^arn:(aws|aws-cn|aws-us-gov):bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/[A-Za-z0-9_-]+$')
    allowed_connectors: list[ConnectionId]
    runtime_role_arn: str | None = Field(default=None, pattern=r'^arn:(aws|aws-cn|aws-us-gov):iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$')
    network_configuration: dict[str, Any] = Field(default_factory=lambda: {'networkMode': 'PUBLIC'})
    secret_arn_prefix: str | None = None
    credentials: dict[ConnectionId, list[dict[str, Any]]] = Field(default_factory=dict)

    @field_validator('network_configuration')
    @classmethod
    def valid_network(cls, value):
        return validate_network_configuration(value)

    @model_validator(mode='after')
    def binding_contract(self):
        if len(set(self.allowed_connectors)) != len(self.allowed_connectors):
            raise ValueError('Supply distinct allowed connector IDs.')
        if self.secret_arn_prefix is not None and not _SECRET_ARN.fullmatch(self.secret_arn_prefix):
            raise ValueError('Supply the project Secrets Manager ARN prefix.')
        try:
            shape = control_model().operation_model('CreateGatewayTarget').input_shape.members['credentialProviderConfigurations']
            for providers in self.credentials.values():
                validate_native(providers, shape)
                provider = providers[0]
                kind = provider['credentialProviderType']
                details = provider.get('credentialProvider', {})
                if kind not in {'OAUTH', 'API_KEY', 'GATEWAY_IAM_ROLE'}:
                    raise ValueError('Shared registrations require project service credentials.')
                if kind == 'OAUTH' and details.get('oauthCredentialProvider', {}).get('grantType') != 'CLIENT_CREDENTIALS':
                    raise ValueError('Shared OAuth registrations require explicit client credentials.')
                expected = {'OAUTH': 'oauthCredentialProvider', 'API_KEY': 'apiKeyCredentialProvider',
                            'GATEWAY_IAM_ROLE': 'iamCredentialProvider'}.get(kind)
                if expected and set(details) != {expected}:
                    raise ValueError('Supply the matching native credential provider.')
                if not expected and details:
                    raise ValueError('Supply the matching native credential provider.')
        except Exception:
            raise ValueError('Supply native trusted Gateway credential provider configurations.') from None
        return self


def validate_project_bindings(bindings: dict[str, ProjectConnections]):
    """A Gateway and secret namespace belong to one project."""
    seen = []
    for binding in bindings.values():
        for other in seen:
            if binding.gateway_arn == other.gateway_arn:
                raise ValueError('Each project requires a dedicated Gateway.')
            left, right = binding.secret_arn_prefix, other.secret_arn_prefix
            if left and right and (left.startswith(right) or right.startswith(left)):
                raise ValueError('Projects require distinct secret ARN prefixes.')
        seen.append(binding)
