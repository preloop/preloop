"""Async httpx defaults for MCP Streamable HTTP integration tests."""

from __future__ import annotations

import httpx

from tests.integration.http_client import (
    _INTEGRATION_LIMITS,
    integration_ssl_context,
)

# Match mcp.shared._httpx_utils defaults without importing the mcp package from
# modules that UI integration tests also load.
_STREAMABLE_MCP_TIMEOUT = 30.0
_STREAMABLE_MCP_READ_TIMEOUT = 300.0


def create_streamable_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Create an httpx client tuned for MCP Streamable HTTP transport."""
    if timeout is None:
        timeout = httpx.Timeout(
            _STREAMABLE_MCP_TIMEOUT, read=_STREAMABLE_MCP_READ_TIMEOUT
        )

    merged_headers = {"Connection": "close"}
    if headers:
        merged_headers.update(headers)

    transport = httpx.AsyncHTTPTransport(
        http2=False,
        limits=_INTEGRATION_LIMITS,
        retries=0,
        verify=integration_ssl_context(),
    )
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=timeout,
        headers=merged_headers,
        auth=auth,
    )
