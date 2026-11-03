"""Authenticated MCP boundary; only server-origin forms supply plan decisions."""

from collections.abc import Awaitable
from functools import partial
from hashlib import sha256
import json
from typing import Any
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from daeri.core.access import AuthenticatedUser
from daeri.core.endpoints import mcp_endpoint
from daeri.core.approval import ApprovalResponse
from daeri.core.errors import StoreError
from daeri.infrastructure.auth import ProjectAccessToken


class _SessionVerifier:
    """Bind SDK sessions to verified authority; keep the real actor in token.user."""

    def __init__(self, verifier):
        self.verifier = verifier

    async def verify_token(self, raw):
        token = await self.verifier.verify_token(raw)
        if not isinstance(token, ProjectAccessToken):
            return None
        authority = [token.user.actor_id, sorted(token.user.project_ids), sorted(set(token.scopes))]
        subject = sha256(json.dumps(authority, separators=(',', ':')).encode()).hexdigest()
        return token.model_copy(update={'subject': subject})


class DecisionForm(BaseModel):
    model_config = ConfigDict(extra='forbid')

    # The SDK accepts primitive annotations; expose and enforce this enum separately.
    decision: str = Field(
        title='계획에 대한 결정',
        description='approve: 현재 계획 승인 / revise: 수정 요청 / reject: 거절',
        json_schema_extra={'enum': ['approve', 'revise', 'reject']},
    )
    response: str = Field(default='', title='수정 사항·추가 조건',
                          description='조건을 입력하면 승인 선택이어도 수정 계획을 다시 확인합니다.')

    @field_validator('decision')
    @classmethod
    def supported_decision(cls, value):
        if value not in ('approve', 'revise', 'reject'):
            raise ValueError('Select a plan decision.')
        return value


def _user(ctx: Context) -> AuthenticatedUser:
    # A stateful MCP session can outlive its first token. Read this POST's identity.
    request = ctx.request_context.request
    token = getattr(getattr(request, 'user', None), 'access_token', None)
    if not isinstance(token, ProjectAccessToken):
        raise StoreError('AUTHENTICATION_REQUIRED', 'Sign in through the host MCP connection.')
    token.user.contexts()
    return token.user


async def _result(operation: Awaitable[Any], *, projects=False) -> CallToolResult:
    try:
        data = await operation
        if projects:
            data = {'projects': data}
        return CallToolResult(
            content=[TextContent(type='text', text=json.dumps(data, ensure_ascii=False))],
            structuredContent=data,
        )
    except StoreError as error:
        data = {'error': {'code': error.code, 'message': error.message}}
        for name in ('task_id', 'turn_id'):
            if getattr(error, name, None) is not None:
                data[name] = getattr(error, name)
    except Exception:
        data = {'error': {'code': 'SERVICE_UNAVAILABLE',
                          'message': 'The operation failed. Check the current task before retrying.'}}
    return CallToolResult(
        content=[TextContent(type='text', text=json.dumps(data, ensure_ascii=False))],
        structuredContent=data, isError=True,
    )


def build_mcp(agent, token_verifier: TokenVerifier, issuer: str, resource_url: str,
              scopes: list[str] | tuple[str, ...]) -> FastMCP:
    endpoint = urlsplit(mcp_endpoint(resource_url))
    server = FastMCP(
        'DAERI', token_verifier=_SessionVerifier(token_verifier),
        auth=AuthSettings(issuer_url=issuer, resource_server_url=resource_url,
                          required_scopes=list(scopes), validate_token_resource=True),
        stateless_http=False, json_response=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[endpoint.netloc, '127.0.0.1:*', 'localhost:*'] if endpoint.query else [endpoint.netloc],
            allowed_origins=[endpoint.scheme + '://' + endpoint.netloc],
        ),
        instructions=(
            'DAERI 공용 서비스입니다. 허용된 프로젝트를 선택하고 업무를 요청하세요. '
            '계획 대기에는 daeri_confirm을 사용하세요. 사용자 신원은 로그인 토큰에서, '
            '승인 결정은 실제 사용자 확인창 응답에서 서버가 연결합니다.'
        ),
    )
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=readonly)
    async def daeri_projects(ctx: Context) -> CallToolResult:
        """현재 회사 로그인으로 접근할 수 있는 프로젝트를 조회한다."""
        return await _result(anyio.to_thread.run_sync(partial(agent.projects, _user(ctx))), projects=True)

    @server.tool(annotations=readonly)
    async def daeri_tasks(project_id: str, ctx: Context, cursor: str | None = None,
                          limit: int = 20) -> CallToolResult:
        """선택한 프로젝트의 자신의 기존 업무를 조회한다."""
        return await _result(anyio.to_thread.run_sync(
            partial(agent.tasks, _user(ctx), project_id, cursor=cursor, limit=limit)))

    @server.tool()
    async def daeri_work(project_id: str, request: str, ctx: Context,
                         task_id: str | None = None) -> CallToolResult:
        """업무를 요청하거나 같은 업무에 후속 요청·피드백을 전달한다."""
        return await _result(anyio.to_thread.run_sync(
            partial(agent.work, _user(ctx), project_id, request, task_id=task_id)))

    @server.tool(annotations=readonly)
    async def daeri_status(project_id: str, task_id: str, ctx: Context) -> CallToolResult:
        """업무의 현재 계획·실제 확인·대기 상태를 조회한다."""
        return await _result(anyio.to_thread.run_sync(partial(agent.status, _user(ctx), project_id, task_id)))

    @server.tool()
    async def daeri_confirm(project_id: str, task_id: str, ctx: Context) -> CallToolResult:
        """저장된 현재 계획을 실제 사용자의 호스트 확인창에 표시하고 명시적 결정을 기다린다."""
        async def ask(plan: str) -> ApprovalResponse | None:
            try:
                with anyio.fail_after(300):
                    answer = await ctx.elicit(message=plan, schema=DecisionForm)
                if answer.action == 'accept':
                    return ApprovalResponse(answer.data.decision, answer.data.response)
            except (McpError, ValidationError, TimeoutError, ValueError,
                    anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass
            return None

        return await _result(agent.confirm(_user(ctx), project_id, task_id, ask))

    @server.tool()
    async def daeri_recover(project_id: str, task_id: str, ctx: Context) -> CallToolResult:
        """이미 보관된 원본 응답을 업무 상태에 반영해 복구한다."""
        return await _result(anyio.to_thread.run_sync(partial(agent.recover, _user(ctx), project_id, task_id)))

    return server
