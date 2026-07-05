"""Tests for MCP Streamable HTTP httpx defaults."""

import httpx

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
