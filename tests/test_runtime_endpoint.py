"""Public connection URLs distinguish a proxy path from the container's /mcp."""

import pytest

from daeri.core.endpoints import mcp_endpoint, runtime_endpoint


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/daeri-1234567890'
URL = 'https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012%3Aruntime%2Fdaeri-1234567890/invocations?qualifier=RELEASE_V2'


def test_runtime_endpoint_encodes_resource_and_release_selector():
    assert runtime_endpoint(ARN, 'RELEASE_V2') == URL
    assert mcp_endpoint(URL) == URL
    assert mcp_endpoint('https://daeri.example/mcp') == 'https://daeri.example/mcp'


@pytest.mark.parametrize('url', [
    'https://daeri.example/mcp?secret=private',
    URL + '&token=private', URL + '&qualifier=OTHER',
    URL.replace('amazonaws.com', 'amazonaws.com.attacker.test'),
    URL.replace('us-east-1.amazonaws.com', 'us-west-2.amazonaws.com'),
    URL.replace('RELEASE_V2', 'bad/route'),
    URL.replace('/invocations?', '/wrong?'),
    URL.replace('runtime%2F', 'harness%2F'),
    URL.replace('?qualifier=RELEASE_V2', ''),
])
def test_runtime_endpoint_rejects_wrong_resource_and_unrelated_query_fields(url):
    with pytest.raises(ValueError):
        mcp_endpoint(url)
