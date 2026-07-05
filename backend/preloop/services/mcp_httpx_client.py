"""httpx defaults for MCP Streamable HTTP clients."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

# Review/staging hosts often share a wildcard certificate (*.test.preloop.ai).
# HTTP/2 connection coalescing can reuse a TLS session negotiated for one
# hostname while follow-up requests target another, which nginx rejects with
# 421 Misdirected Request. Disable keep-alive pooling for MCP sessions.
_STREAMABLE_MCP_LIMITS = httpx.Limits(max_keepalive_connections=0, max_connections=10)


def create_streamable_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Create an httpx client tuned for MCP Streamable HTTP transport.

    Streamable HTTP keeps session state across POST/SSE exchanges on one client.
    Following redirects (for example ingress ``www`` canonicalization) can reuse
    an HTTP/2 connection for a different authority and surface ``421
    Misdirected Request`` behind shared TLS termination. MCP clients also cannot
    safely follow redirects mid-handshake, so redirects are disabled here.
    """
    if timeout is None:
        timeout = httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)

    kwargs: dict[str, Any] = {
        "follow_redirects": False,
        "http2": False,
        "timeout": timeout,
        "limits": _STREAMABLE_MCP_LIMITS,
    }
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)
