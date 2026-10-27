"""HTTP defaults and response policy for the MCP SDK v2 transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2 as httpx
from mcp import types
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

# Review/staging hosts often share a wildcard certificate (*.test.preloop.ai).
# HTTP/2 connection coalescing can reuse a TLS session negotiated for one
# hostname while follow-up requests target another, which nginx rejects with
# 421 Misdirected Request. Disable keep-alive pooling for MCP sessions.
_STREAMABLE_MCP_LIMITS = httpx.Limits(max_keepalive_connections=0, max_connections=10)


@dataclass
class MCPResponsePolicy:
    """Remember whether discovery received a legacy-compatible rejection.

    The SDK maps HTTP errors to JSON-RPC errors, losing the status needed for
    transport-specific fallback and OAuth recovery. Keep that distinction at
    the HTTP boundary. No response bodies or credentials are retained.
    """

    legacy_probe_rejected: bool = False

    async def on_response(self, response: httpx.Response) -> None:
        """Reject redirects/auth failures before the SDK can consume them."""
        if (
            response.is_redirect
            or response.status_code in {401, 403, 429}
            or response.is_server_error
        ):
            response.raise_for_status()
        if response.request.headers.get("mcp-method") != "server/discover":
            return
        self.legacy_probe_rejected = False
        if response.status_code not in {200, 400}:
            return
        if response.status_code == 200 and (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            # Successful discovery may be SSE: leave its incremental stream
            # to the SDK instead of blocking here until the server closes it.
            return
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if response.status_code == 200:
            # Legacy JSON-RPC servers commonly report an unknown method with
            # HTTP 200. A modern HTTP server uses 404 for METHOD_NOT_FOUND.
            # Explicit modern version evidence must never permit a downgrade.
            self.legacy_probe_rejected = (
                isinstance(body, dict)
                and body.get("jsonrpc") == "2.0"
                and code == types.METHOD_NOT_FOUND
                and response.headers.get("mcp-protocol-version")
                not in MODERN_PROTOCOL_VERSIONS
            )
            return
        modern_error = (
            isinstance(body, dict)
            and body.get("jsonrpc") == "2.0"
            and isinstance(code, int)
            and code
            in {
                types.HEADER_MISMATCH,
                types.MISSING_REQUIRED_CLIENT_CAPABILITY,
                types.UNSUPPORTED_PROTOCOL_VERSION,
                types.METHOD_NOT_FOUND,
            }
        )
        self.legacy_probe_rejected = not modern_error


def create_streamable_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    *,
    response_policy: MCPResponsePolicy | None = None,
) -> httpx.AsyncClient:
    """Create an httpx client tuned for MCP Streamable HTTP transport.

    Streamable HTTP keeps session state across POST/SSE exchanges on one client.
    Following redirects (for example ingress ``www`` canonicalization) can reuse
    an HTTP/2 connection for a different authority and surface ``421
    Misdirected Request`` behind shared TLS termination. MCP clients also cannot
    safely follow redirects mid-handshake, so redirects are disabled here.
    """
    if timeout is None:
        timeout = httpx.Timeout(30.0, read=300.0)

    kwargs: dict[str, Any] = {
        "follow_redirects": False,
        "http2": False,
        "timeout": timeout,
        "limits": _STREAMABLE_MCP_LIMITS,
        # SDK v2 follows same-origin redirects itself, regardless of the
        # client's follow_redirects flag. Reject them before it can replay POST.
        "event_hooks": {
            "response": [(response_policy or MCPResponsePolicy()).on_response]
        },
    }
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)
