"""Operator inputs for project-scoped Memory archiving and reconstruction."""

from contextlib import contextmanager, ExitStack

from pydantic import Field, field_validator

from daeri.config.cloud import CloudSettings
from daeri.core.errors import StoreError
from daeri.infrastructure.memory_archive import _memory_id


class MemoryArchiveSettings(CloudSettings):
    project_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
    source_memory_id: str = 'receipts'

    @field_validator('source_memory_id')
    @classmethod
    def source_identifier(cls, value):
        try:
            return value if value == 'receipts' else _memory_id(value)
        except StoreError:
            raise ValueError('Supply an AgentCore Memory identifier or receipts.') from None


@contextmanager
def build_memory_archive(settings):
    import boto3
    from botocore.config import Config
    from daeri.infrastructure.memory_archive import MemoryArchive

    with ExitStack() as resources:
        session = boto3.Session()
        def client(service):
            value = session.client(service, region_name=settings.region,
                config=Config(retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=60))
            resources.callback(value.close)
            return value
        yield MemoryArchive(client('bedrock-agentcore'), client('s3'), client('bedrock-agentcore-control'),
                            settings.storage.bucket, settings.storage.prefix)
