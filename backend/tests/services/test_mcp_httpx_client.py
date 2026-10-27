"""Tests for MCP Streamable HTTP httpx defaults."""

from typing import AsyncIterator

import httpx2 as httpx
import pytest

from preloop.services.mcp_httpx_client import create_streamable_mcp_http_client


def test_create_streamable_mcp_http_client_disables_redirects_and_http2() -> None:
    """MCP transport must not follow redirects or negotiate HTTP/2."""
    client = create_streamable_mcp_http_client(headers={"Authorization": "Bearer t"})
    assert client.follow_redirects is False
    assert client.headers["Authorization"] == "Bearer t"


def test_create_streamable_mcp_http_client_honors_custom_timeout() -> None:
    timeout = httpx.Timeout(10.0, read=120.0)
    client = create_streamable_mcp_http_client(timeout=timeout)
    assert client.timeout.connect == 10.0
    assert client.timeout.read == 120.0


@pytest.mark.asyncio
async def test_discovery_policy_does_not_consume_successful_sse() -> None:
    """The SDK must receive SSE discovery incrementally, without awaiting EOF."""
    from preloop.services.mcp_httpx_client import MCPResponsePolicy

    class OpenEventStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise AssertionError("Response policy must not consume the SSE stream")
            yield b""  # pragma: no cover

    response = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        stream=OpenEventStream(),
        request=httpx.Request(
            "POST", "http://example.com/mcp", headers={"Mcp-Method": "server/discover"}
        ),
    )
    policy = MCPResponsePolicy()
    await policy.on_response(response)
    assert not policy.legacy_probe_rejected
    assert not response.is_stream_consumed
