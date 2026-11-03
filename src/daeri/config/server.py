"""Inert managed-login settings and explicit remote service composition."""

from contextlib import ExitStack
from dataclasses import dataclass, field
import re

from pydantic import Field, field_validator, model_validator

from daeri.config.cloud import CloudServices, CloudSettings, _Settings, build_cloud
from daeri.core.errors import StoreError
from daeri.core.projects import valid_harness_release
from daeri.core.endpoints import mcp_endpoint
from daeri.infrastructure.auth import JwtTokenVerifier, https_url


class AuthSettings(_Settings):
    issuer: str
    audience: str = Field(min_length=1)
    project_claim: str = 'projects'
    required_scopes: list[str] = Field(default_factory=lambda: ['daeri/use'])

    @field_validator('issuer')
    @classmethod
    def validate_issuer(cls, value):
        return https_url(value)

    @field_validator('audience', 'project_claim')
    @classmethod
    def validate_claim_settings(cls, value):
        if not value.strip() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError('Supply an explicit resource audience and project claim.')
        return value

    @field_validator('required_scopes')
    @classmethod
    def validate_scopes(cls, value):
        if (not value or len(set(value)) != len(value)
                or any(not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]+', item) for item in value)):
            raise ValueError('Supply distinct required OAuth scopes.')
        return value


class ServiceSettings(CloudSettings):
    auth: AuthSettings
    approval_hook_arn: str

    @model_validator(mode='after')
    def delegated_token_audience(self):
        if self.harness_oauth_audience is not None and self.harness_oauth_audience != self.auth.audience:
            raise ValueError('The service and delegated harness must accept the same user-token audience.')
        return self

    @field_validator('approval_hook_arn')
    @classmethod
    def validate_hook_arn(cls, value):
        if not re.fullmatch(r'arn:[a-z0-9-]+:lambda:[a-z0-9-]+:[0-9]{12}:function:'
                            r'[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_-]+|:\$LATEST)?', value):
            raise ValueError('Supply the managed approval hook Lambda ARN.')
        return value


class ServerSettings(ServiceSettings):
    public_url: str

    @field_validator('public_url')
    @classmethod
    def validate_public_url(cls, value):
        return mcp_endpoint(value)


class HarnessReadiness:
    """Check the actual saved harness route before a public invocation claims a turn."""

    def __init__(self, client, approval_hook_arn):
        self.client, self.approval_hook_arn = client, approval_hook_arn

    def check(self, route):
        try:
            arn, endpoint, version = route.harness_arn, route.harness_endpoint, route.harness_version
            if not isinstance(arn, str) or ':harness/' not in arn or not valid_harness_release(endpoint, version):
                raise ValueError('Invalid saved harness release')
            harness_id = arn.rsplit('/', 1)[1]
            release = self.client.get_harness_endpoint(harnessId=harness_id, endpointName=endpoint)['endpoint']
            if (release.get('harnessId') != harness_id or release.get('endpointName') != endpoint
                    or release.get('status') != 'READY' or release.get('liveVersion') != version
                    or release.get('targetVersion') != version):
                raise ValueError('The saved release endpoint changed')
            harness = self.client.get_harness(harnessId=harness_id, harnessVersion=version)['harness']
            if harness['arn'] != arn or harness['status'] != 'READY' or harness.get('harnessVersion') != version:
                raise ValueError('Harness release is not ready')
            hooks = [item['beforeToolCall']['target'].get('lambda', {})
                     for item in harness.get('hooks', []) if 'beforeToolCall' in item]
            if not any(item.get('arn') == self.approval_hook_arn and item.get('failureMode') == 'deny'
                       for item in hooks):
                raise ValueError('Mandatory deny hook is absent')
            tools = [item for item in harness.get('tools', []) if item.get('name') == 'confirm_plan']
            if len(tools) != 1 or tools[0].get('type') != 'inline_function':
                raise ValueError('The real-user plan confirmation tool is absent')
            schema = tools[0]['config']['inlineFunction']['inputSchema']
            if (schema.get('type') != 'object' or 'plan' not in schema.get('required', [])
                    or schema.get('properties', {}).get('plan', {}).get('type') != 'string'):
                raise ValueError('The plan confirmation input is invalid')
        except Exception:
            raise StoreError('HARNESS_NOT_READY',
                             'The saved harness release and required approval configuration could not be verified.') from None


@dataclass
class ServerServices:
    cloud: CloudServices
    agent: object
    mcp: object
    token_verifier: JwtTokenVerifier
    _resources: ExitStack = field(repr=False)

    def close(self):
        self._resources.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def build_server(settings: ServerSettings, *, session=None) -> ServerServices:
    """Compose managed clients and the MCP resource server without authenticating users."""
    import boto3
    from botocore.config import Config

    from daeri.application.agent import AgentService
    from daeri.infrastructure.artifacts import ArtifactService
    from daeri.infrastructure.mcp_server import build_mcp

    session = session if session is not None else boto3.Session()
    with ExitStack() as resources:
        cloud = build_cloud(settings, session=session)
        resources.callback(cloud.close)
        control = session.client('bedrock-agentcore-control', region_name=settings.region,
                                 config=Config(retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=60))
        resources.callback(control.close)
        readiness = HarnessReadiness(control, settings.approval_hook_arn)
        artifacts = ArtifactService(cloud.store.client, settings.storage.bucket, settings.storage.prefix)
        cloud.workflow.output_location = artifacts.location
        agent = AgentService(cloud.workflow, check_harness=readiness.check, artifacts=artifacts)
        verifier = JwtTokenVerifier(settings.auth.issuer, settings.auth.audience, settings.public_url,
                                    project_claim=settings.auth.project_claim)
        mcp = build_mcp(agent, verifier, settings.auth.issuer, settings.public_url, settings.auth.required_scopes,
                        harness_audience=settings.harness_oauth_audience)
        return ServerServices(cloud, agent, mcp, verifier, resources.pop_all())
