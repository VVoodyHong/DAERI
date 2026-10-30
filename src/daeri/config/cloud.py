"""Inert cloud settings and explicit AWS service composition."""

from contextlib import ExitStack
from dataclasses import dataclass, field
import json
import re
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from daeri.application.cloud import CloudWorkflow, SharedKnowledgeProcessor
from daeri.config.setup_yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.infrastructure.agentcore import AgentCoreHarness
from daeri.infrastructure.cloud_models import BedrockEmbedder, BedrockExperienceExtractor, OpenAIExperienceExtractor
from daeri.infrastructure.cloud_postgres import AuroraCloudStore
from daeri.infrastructure.embedding import OpenAICompatibleEmbedder, validate_api_endpoint


_SECRET_ARN = r'arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+'
_IDENTITY_KEY_ARN = (r'arn:[a-z0-9-]+:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:'
                     r'token-vault/[A-Za-z0-9.-]+/apikeycredentialprovider/[A-Za-z0-9.-]+')
SecretArn = Annotated[str, StringConstraints(pattern='^' + _SECRET_ARN + '$')]
Text = Annotated[str, StringConstraints(min_length=1, max_length=256,
    pattern=r'^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f])?$')]


class _Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


class DatabaseSettings(_Settings):
    resource_arn: Annotated[str, StringConstraints(
        pattern=r'^arn:[a-z0-9-]+:rds:[a-z0-9-]+:[0-9]{12}:cluster:[A-Za-z0-9-]+$')]
    secret_arn: SecretArn
    name: Text


class BedrockKnowledgeSettings(_Settings):
    provider: Literal['bedrock']
    model: Text


class ApiModelSettings(_Settings):
    provider: Literal['api']
    endpoint: str
    model: Text
    api_key_secret_arn: SecretArn

    @field_validator('endpoint')
    @classmethod
    def validate_endpoint(cls, value):
        try:
            return validate_api_endpoint(value)
        except StoreError:
            raise ValueError('Supply the exact HTTPS API request URL without credentials.') from None


class KeywordEmbeddingSettings(_Settings):
    provider: Literal['keyword_only'] = 'keyword_only'


class BedrockEmbeddingSettings(_Settings):
    provider: Literal['bedrock']
    model: Literal['amazon.titan-embed-text-v2:0'] = 'amazon.titan-embed-text-v2:0'
    dimensions: Annotated[int, Field(strict=True)] = 1024

    @field_validator('dimensions')
    @classmethod
    def validate_dimensions(cls, value):
        if value not in (256, 512, 1024):
            raise ValueError('Titan V2 supports 256, 512, or 1024 dimensions.')
        return value


class ApiEmbeddingSettings(ApiModelSettings):
    dimensions: Annotated[int, Field(ge=1, le=8192)] | None = None


class CloudSettings(_Settings):
    region: Annotated[str, StringConstraints(pattern=r'^[a-z]{2}(?:-[a-z]+)+-[0-9]+$')] | None = None
    harness_arn: Annotated[str, StringConstraints(
        pattern=r'^arn:[a-z0-9-]+:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:harness/[A-Za-z][A-Za-z0-9_]{0,39}-[A-Za-z0-9]{10}$')]
    harness_model: dict[str, Any] | None = None
    database: DatabaseSettings
    knowledge_model: Annotated[BedrockKnowledgeSettings | ApiModelSettings, Field(discriminator='provider')]
    embedding: Annotated[KeywordEmbeddingSettings | BedrockEmbeddingSettings | ApiEmbeddingSettings,
                         Field(discriminator='provider')] = Field(default_factory=KeywordEmbeddingSettings)

    @field_validator('harness_model')
    @classmethod
    def reject_inline_secrets(cls, value):
        def check(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if key.lower().replace('_', '') in {
                        'apikey', 'accesskey', 'secretkey', 'token', 'password', 'secret', 'authorization', 'credentials'
                    }:
                        raise ValueError('Use credential references, never inline credentials.')
                    if key == 'apiKeyArn' and (not isinstance(child, str) or not re.fullmatch(_IDENTITY_KEY_ARN, child)):
                        raise ValueError('Use an AgentCore Identity API key credential provider ARN for apiKeyArn.')
                    check(child)
            elif isinstance(item, list):
                for child in item:
                    check(child)
        check(value)
        return value

    @classmethod
    def from_yaml(cls, path):
        try:
            return cls.model_validate(read_yaml(path))
        except ValidationError:
            raise StoreError('INVALID_INPUT', 'Cloud settings are invalid; check supported fields, providers, URLs, and ARN references.') from None


class _Secrets:
    def __init__(self, client, references):
        self.client, self.references = client, references

    def get(self, reference):
        try:
            token = self.client.get_secret_value(SecretId=self.references[reference])['SecretString']
            if not isinstance(token, str) or not token.strip() or any(ord(char) < 32 for char in token):
                raise ValueError('Invalid SecretString')
            return token
        except Exception:
            raise StoreError('AUTH_REQUIRED', 'The configured Secrets Manager SecretString could not be resolved.') from None


@dataclass
class CloudServices:
    store: AuroraCloudStore
    workflow: CloudWorkflow
    knowledge: SharedKnowledgeProcessor
    _resources: ExitStack = field(repr=False)

    def close(self):
        self._resources.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def build_cloud(settings: CloudSettings, *, session=None, http_client=None) -> CloudServices:
    """Create clients only here; callers explicitly initialize the store and close services."""
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import ParamValidationError
        from botocore.session import get_session
        from botocore.validate import validate_parameters
    except ImportError:
        raise StoreError('SETUP_REQUIRED', 'Install the cloud extra: uv sync --extra cloud (or pip install "daeri-agent[cloud]").') from None

    if settings.harness_model is not None:
        try:
            shape = get_session().get_service_model('bedrock-agentcore').operation_model('InvokeHarness').input_shape.members['model']
            validate_parameters(settings.harness_model, shape)
            json.dumps(settings.harness_model, allow_nan=False, default=float)
            provider, model = next(iter(settings.harness_model.items()))
            for name, value in model.items():
                field = shape.members[provider].members[name]
                metadata = field.metadata
                if (('enum' in metadata and value not in metadata['enum'])
                        or (field.type_name in ('integer', 'float') and
                            (isinstance(value, bool) or not metadata.get('min', value) <= value <= metadata.get('max', value)))):
                    raise ValueError('Invalid model field')
        except (ParamValidationError, TypeError, ValueError):
            raise StoreError('INVALID_INPUT', 'harness_model must match the installed SDK InvokeHarness model union.') from None

    session = session if session is not None else boto3.Session()
    with ExitStack() as resources:
        clients = {}

        def client(service):
            if service not in clients:
                value = session.client(service, region_name=settings.region, config=Config(
                    retries={'total_max_attempts': 1}, connect_timeout=10,
                    read_timeout=300 if service == 'bedrock-agentcore' else 60))
                resources.callback(value.close)
                clients[service] = value
            return clients[service]

        database = settings.database
        store = AuroraCloudStore(client('rds-data'), database.resource_arn, database.secret_arn, database.name)
        harness = AgentCoreHarness(client('bedrock-agentcore'), settings.harness_arn, settings.harness_model)
        api_settings = {'knowledge': settings.knowledge_model, 'embedding': settings.embedding}
        references = {'credential:cloud.' + role: selected.api_key_secret_arn
                      for role, selected in api_settings.items() if selected.provider == 'api'}
        if references:
            secrets = _Secrets(client('secretsmanager'), references)
            http_client = http_client if http_client is not None else resources.enter_context(httpx.Client())

        selected = settings.embedding
        embedder = None
        if selected.provider == 'bedrock':
            embedder = BedrockEmbedder(client('bedrock-runtime'), selected.model, dimensions=selected.dimensions)
        elif selected.provider == 'api':
            config = {'provider': 'api', 'adapter': 'openai_compatible', 'endpoint': selected.endpoint,
                      'model': selected.model, 'credential_ref': 'credential:cloud.embedding'}
            if selected.dimensions is not None:
                config['dimensions'] = selected.dimensions
            embedder = OpenAICompatibleEmbedder(config, credentials=secrets, client=http_client)

        selected = settings.knowledge_model
        if selected.provider == 'bedrock':
            extractor = BedrockExperienceExtractor(client('bedrock-runtime'), selected.model)
        else:
            extractor = OpenAIExperienceExtractor(http_client, selected.endpoint, selected.model,
                'credential:cloud.knowledge', secrets.get)
        return CloudServices(store, CloudWorkflow(store, harness, embedder),
                             SharedKnowledgeProcessor(store, extractor, embedder), resources.pop_all())
