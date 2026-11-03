"""Project-owned managed harness releases exposed as Gateway agent tools."""

from pydantic import Field, model_validator

from daeri.config.cloud import _Settings
from daeri.core.projects import valid_harness_release
from daeri.infrastructure.project_catalog import _HARNESS_ARN


class SubagentSettings(_Settings):
    description: str = Field(min_length=1, max_length=1024)
    harness_arn: str
    harness_endpoint: str
    harness_version: str

    @model_validator(mode='after')
    def release(self):
        if (not _HARNESS_ARN.fullmatch(self.harness_arn)
                or not valid_harness_release(self.harness_endpoint, self.harness_version)):
            raise ValueError('Supply a named immutable managed harness release.')
        return self


class DelegationSettings(_Settings):
    project_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
    gateway_arn: str = Field(pattern=r'^arn:[a-z0-9-]+:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/[A-Za-z0-9_-]+$')
    target_name: str = Field(default='subagents', pattern=r'^([0-9a-zA-Z][-]?){1,100}$')
    lambda_arn: str = Field(pattern=r'^arn:[a-z0-9-]+:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+$')
    subagents: dict[str, SubagentSettings] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def tool_names(self):
        import re
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', name) for name in self.subagents):
            raise ValueError('Use stable subagent tool names.')
        return self
