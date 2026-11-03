"""Inert cloud settings and explicit AWS service composition."""

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from daeri.application.cloud import CloudWorkflow
from daeri.config.yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.infrastructure.agentcore import AgentCoreHarness
from daeri.infrastructure.cloud_s3 import S3CloudStore, normalize_storage_prefix, validate_storage_bucket
from daeri.infrastructure.project_catalog import SsmProjectCatalog, normalize_project_prefix


class _Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


class StorageSettings(_Settings):
    bucket: str
    prefix: str = 'daeri/'

    @field_validator('bucket')
    @classmethod
    def validate_bucket(cls, value):
        try:
            return validate_storage_bucket(value)
        except StoreError:
            raise ValueError('Supply a supported S3 bucket name.') from None

    @field_validator('prefix')
    @classmethod
    def validate_prefix(cls, value):
        try:
            return normalize_storage_prefix(value)
        except StoreError:
            raise ValueError('Supply a relative S3 storage prefix.') from None


class CloudSettings(_Settings):
    region: Annotated[str, StringConstraints(pattern=r'^[a-z]{2}(?:-[a-z]+)+-[0-9]+$')] | None = None
    project_parameter_prefix: str = '/daeri/projects/'
    storage: StorageSettings
    harness_oauth_audience: str | None = Field(default=None, min_length=1)

    @field_validator('project_parameter_prefix')
    @classmethod
    def validate_project_prefix(cls, value):
        try:
            return normalize_project_prefix(value)
        except StoreError:
            raise ValueError('Supply an absolute project parameter prefix.') from None

    @field_validator('harness_oauth_audience')
    @classmethod
    def accepted_harness_audience(cls, value):
        if value is not None and (not value.strip() or any(char.isspace() for char in value)):
            raise ValueError('Supply the accepted user-token audience for the harness.')
        return value

    @classmethod
    def from_yaml(cls, path):
        try:
            return cls.model_validate(read_yaml(path))
        except ValidationError:
            raise StoreError('INVALID_INPUT', 'Cloud settings are invalid; check supported fields and storage locations.') from None


@dataclass
class CloudServices:
    store: S3CloudStore
    workflow: CloudWorkflow
    _resources: ExitStack = field(repr=False)

    def close(self):
        self._resources.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def build_cloud(settings: CloudSettings, *, session=None) -> CloudServices:
    """Create clients without making service requests; close the returned services after use."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise StoreError('SETUP_REQUIRED', 'Install the project dependencies with uv sync or pip install daeri-agent.') from None

    session = session if session is not None else boto3.Session()
    with ExitStack() as resources:
        def client(service):
            options = {}
            if service == 'bedrock-agentcore' and settings.harness_oauth_audience:
                from botocore import UNSIGNED
                options['signature_version'] = UNSIGNED
            value = session.client(service, region_name=settings.region, config=Config(
                retries={'total_max_attempts': 1}, connect_timeout=10,
                read_timeout=300 if service == 'bedrock-agentcore' else 60, **options))
            resources.callback(value.close)
            return value

        store = S3CloudStore(client('s3'), settings.storage.bucket, settings.storage.prefix)
        catalog = SsmProjectCatalog(client('ssm'), settings.project_parameter_prefix)
        harness_client = client('bedrock-agentcore')
        if settings.harness_oauth_audience:
            from daeri.infrastructure.harness_oauth import OAuthHarnessClient
            harness_client = OAuthHarnessClient(harness_client, audience=settings.harness_oauth_audience)
        harness = AgentCoreHarness(harness_client)
        return CloudServices(store, CloudWorkflow(store, harness, catalog), resources.pop_all())
