"""Public MCP addresses for direct hosting and AgentCore Runtime."""

import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit


_RUNTIME_ARN = re.compile(r'arn:(aws|aws-cn|aws-us-gov):bedrock-agentcore:([a-z0-9-]+):[0-9]{12}:runtime/[A-Za-z][A-Za-z0-9_-]+')
_ENDPOINT = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,47}')


def runtime_endpoint(arn: str, qualifier: str) -> str:
    match = _RUNTIME_ARN.fullmatch(arn)
    if match is None or not _ENDPOINT.fullmatch(qualifier):
        raise ValueError('Supply an AgentCore Runtime ARN and named release endpoint.')
    partition, region = match.groups()
    domain = 'amazonaws.com.cn' if partition == 'aws-cn' else 'amazonaws.com'
    return f'https://bedrock-agentcore.{region}.{domain}/runtimes/{quote(arn, safe="")}/invocations?qualifier={qualifier}'


def mcp_endpoint(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('Supply a public HTTPS MCP endpoint.')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise ValueError('Supply a public HTTPS MCP endpoint without credentials or fragment.')
    parsed.port
    if parsed.path == '/mcp' and not parsed.query:
        return value
    match = re.fullmatch(r'/runtimes/([^/]+)/invocations', parsed.path)
    query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    if match and len(query) == 1 and query[0][0] == 'qualifier':
        expected = runtime_endpoint(unquote(match[1]), query[0][1])
        if expected == value:
            return value
    raise ValueError('Supply a direct /mcp endpoint or an AgentCore Runtime release URL.')
