"""MCP Client Pool for managing connections to external MCP servers.

This module provides connection pooling and management for external MCP servers.
It maintains persistent HTTP connections and handles authentication.
"""

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import UUID

import httpx as legacy_httpx
import httpx2 as httpx
from mcp import MCPError, types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from preloop.services.mcp_httpx_client import (
    MCPResponsePolicy,
    create_streamable_mcp_http_client,
)

logger = logging.getLogger(__name__)


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Return the first meaningful leaf from a (possibly nested) exception group.

    The MCP streamable-HTTP client runs inside AnyIO task groups, so an upstream
    failure (connection error, HTTP 502, ``McpError: Session terminated``) surfaces
    wrapped in one or more ``ExceptionGroup``/``BaseExceptionGroup`` layers. This
    descends those layers and returns the first ``Exception`` leaf (preferred), or
    the first leaf of any kind, so callers can surface an actionable cause instead
    of the opaque "unhandled errors in a TaskGroup" wrapper.
    """
    if type(exc).__name__ in ("ExceptionGroup", "BaseExceptionGroup"):
        subs = list(getattr(exc, "exceptions", []) or [])
        # Prefer a normal Exception leaf over BaseExceptions (GeneratorExit, etc.).
        for sub in subs:
            leaf = _unwrap_exception_group(sub)
            if isinstance(leaf, Exception):
                return leaf
        for sub in subs:
            leaf = _unwrap_exception_group(sub)
            if leaf is not None:
                return leaf
    return exc


def describe_mcp_connection_error(exc: BaseException) -> str:
    """Explain the failure without exposing request URLs, headers or credentials."""
    cause = _unwrap_exception_group(exc)
    from preloop.services.upstream_mcp_oauth import OAuthError
    from preloop.models.crud.upstream_mcp_oauth import OAuthContextError

    if isinstance(cause, (OAuthError, OAuthContextError)):
        return str(cause)
    if isinstance(cause, (httpx.HTTPStatusError, legacy_httpx.HTTPStatusError)):
        code = cause.response.status_code
        if code in {401, 403}:
            return (
                f"MCP server rejected authentication or authorization (HTTP {code}). "
                "Configure or reconnect authentication in Preloop, then rescan. "
                "Credentials cached by your local agent may require a separate sign-in."
            )
        if code == 404:
            return (
                "MCP endpoint not found (HTTP 404). Verify the server URL and rescan."
            )
        return (
            f"MCP server returned HTTP {code}. "
            "Check the server endpoint and availability, then rescan."
        )
    if isinstance(
        cause, (httpx.TimeoutException, legacy_httpx.TimeoutException, TimeoutError)
    ):
        return "MCP server timed out. Check server availability and rescan."
    if isinstance(
        cause, (httpx.ConnectError, legacy_httpx.ConnectError, ConnectionError)
    ):
        return (
            "Could not connect to the MCP server. Check its address and network "
            "reachability from Preloop. A localhost endpoint must run where Preloop "
            "is hosted or be exposed through a reachable address."
        )
    return (
        f"MCP connection failed ({type(cause).__name__}). "
        "Verify the endpoint, authentication and MCP protocol, then rescan."
    )


class MCPToolOutcomeUnknownError(ConnectionError):
    """A dispatched tool may have executed; no transport recovery may replay it."""


class UpstreamToolContent(list):
    """Content blocks of an upstream ``CallToolResult``.

    Still a plain list for existing callers, but keeps ``isError`` and
    ``structuredContent`` so the proxy can forward them instead of
    flattening a tool error into a normal result.
    """

    def __init__(
        self,
        items: Any = (),
        *,
        is_error: bool = False,
        structured_content: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(items)
        self.is_error = is_error
        self.structured_content = structured_content


def is_mcp_unavailable_error(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` indicate the upstream MCP server is unreachable/
    unavailable (connection refused, 5xx/502, ``Session terminated``, timeout)
    rather than a genuine tool-level error?

    Used to surface a friendly "server temporarily unavailable, please retry"
    message to agents instead of a raw exception string.

    Prefer typed exceptions over message matching. Message markers are kept
    narrow (multi-word / transport-specific phrases) so tool-level errors that
    merely mention "connection" or "unavailable" are not misclassified.
    """
    exc = _unwrap_exception_group(exc)
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, (httpx.TransportError, legacy_httpx.TransportError)):
        return True
    if isinstance(exc, (httpx.HTTPStatusError, legacy_httpx.HTTPStatusError)):
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", 0) in (429, 500, 502, 503, 504)
    message = str(exc).lower()
    # Avoid bare tokens like "connection" / "unavailable" — those appear in
    # legitimate tool error text and would false-positive as transport failures.
    markers = (
        "session terminated",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "connection refused",
        "connection reset",
        "connection aborted",
        "connection error",
        "temporarily unavailable",
        "server unavailable",
        "timed out",
        "timeout waiting",
        "econnrefused",
        "connect call failed",
        "all connection attempts failed",
        "status code 502",
        "status code 503",
        "status code 504",
        "http 502",
        "http 503",
        "http 504",
    )
    return any(marker in message for marker in markers)


def mcp_client_config_fingerprint(
    url: str,
    auth_type: str = "none",
    auth_config: Optional[Dict[str, Any]] = None,
    transport: str = "http-streaming",
) -> str:
    """Return a stable fingerprint of the connection config of a server.

    The pool compares it on every ``get_client`` call so a changed token,
    URL, auth type or transport rebuilds the cached client. The value only
    lives in process memory and is never logged.
    """
    payload = json.dumps(
        [
            (url or "").rstrip("/"),
            auth_type or "none",
            auth_config or {},
            transport or "http-streaming",
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MCPClient:
    """Client for communicating with an external MCP server over HTTP streaming."""

    def __init__(
        self,
        url: str,
        auth_type: str = "none",
        auth_config: Optional[Dict[str, Any]] = None,
        transport: str = "http-streaming",
    ):
        """Initialize MCP client.

        Args:
            url: Full URL of the MCP server endpoint (e.g., http://localhost:8001/mcp)
            auth_type: Type of authentication (none, bearer, api_key)
            auth_config: Authentication configuration
            transport: Transport protocol (default: http-streaming)
        """
        self.url = url.rstrip("/")
        self.auth_type = auth_type
        self.auth_config = auth_config or {}
        self.transport = transport
        self._session: Optional[ClientSession] = None
        self._exit_stack: Optional[AsyncExitStack] = None
        self._connected = False
        self._protocol_version: str | None = None
        self._protocol_url: str | None = None
        self.oauth_server_id: UUID | None = None
        self.oauth_version: int | None = None
        self._oauth_recovery_version: int | None = None
        self.config_fingerprint = mcp_client_config_fingerprint(
            url, auth_type, auth_config, transport
        )

    def _build_auth(self) -> tuple[Dict[str, str], Optional[httpx.Auth]]:
        """Build request headers and httpx auth for the configured auth type."""
        headers = {}

        # Add authentication headers
        if self.auth_type == "bearer" and "token" in self.auth_config:
            headers["Authorization"] = f"Bearer {self.auth_config['token']}"
        elif self.auth_type == "oauth" and "access_token" in self.auth_config:
            headers["Authorization"] = f"Bearer {self.auth_config['access_token']}"
        elif self.auth_type == "api_key" and "api_key" in self.auth_config:
            key_name = self.auth_config.get("key_name", "X-API-Key")
            headers[key_name] = self.auth_config["api_key"]

        # Create auth object for httpx if using bearer token
        auth = None
        bearer_token = None
        if self.auth_type == "bearer" and "token" in self.auth_config:
            bearer_token = self.auth_config["token"]
        elif self.auth_type == "oauth" and "access_token" in self.auth_config:
            bearer_token = self.auth_config["access_token"]

        if bearer_token:
            # Use httpx.Auth for bearer token
            class BearerAuth(httpx.Auth):
                def __init__(self, token: str):
                    self.token = token

                def auth_flow(self, request):
                    request.headers["Authorization"] = f"Bearer {self.token}"
                    yield request

            auth = BearerAuth(bearer_token)

        return headers, auth

    @asynccontextmanager
    async def _raw_session_context(
        self, *, sse_read_timeout: float = 60.0
    ) -> AsyncIterator[tuple[ClientSession, Any]]:
        """Create a short-lived MCP session in the current task."""
        if self.url != self._protocol_url:
            self._protocol_version = None
        headers, auth = self._build_auth()

        response_policy = MCPResponsePolicy()
        async with create_streamable_mcp_http_client(
            headers=headers if not auth else {},  # Use auth param if bearer
            timeout=httpx.Timeout(30.0, read=sse_read_timeout),
            auth=auth,
            response_policy=response_policy,
        ) as http_client:
            async with streamable_http_client(
                url=self.url, http_client=http_client
            ) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream=read_stream,
                    write_stream=write_stream,
                    read_timeout_seconds=sse_read_timeout,
                ) as session:
                    if (
                        self._protocol_version in MODERN_PROTOCOL_VERSIONS
                        or self._protocol_version is None
                    ):
                        try:
                            await session.discover()
                        except MCPError:
                            # Only a read-only probe with an unrecognized HTTP
                            # 400 permits fallback. Never downgrade recognized
                            # modern errors or retry a dispatched tools/call.
                            if not response_policy.legacy_probe_rejected:
                                raise
                            await session.initialize()
                    else:
                        await session.initialize()
                    self._protocol_version = session.protocol_version
                    self._protocol_url = self.url
                    yield session, session

    async def _load_oauth(self, *, force_version: int | None = None) -> None:
        if self.oauth_server_id is None:
            return
        from preloop.services.upstream_mcp_oauth import UpstreamMCPOAuthService

        snapshot = await UpstreamMCPOAuthService().credentials(
            self.oauth_server_id, force_version=force_version
        )
        self.url = snapshot.url
        self.transport = snapshot.transport
        self.auth_config = snapshot.config
        self.oauth_version = snapshot.version
        if self.auth_config.get("introspection") is not None:
            from preloop.models.schemas.grant_introspection import IntrospectionConfig
            from preloop.services.grant_introspection import grant_introspector

            result = await grant_introspector.evaluate(
                self.auth_config.get("access_token"),
                IntrospectionConfig.model_validate(self.auth_config["introspection"]),
                server_id=str(self.oauth_server_id),
            )
            if result.deny_reason:
                raise PermissionError(
                    "OAuth grant is inactive or lacks the required scope. Reconnect the account."
                )

    @asynccontextmanager
    async def _session_context(
        self, *, sse_read_timeout: float = 60.0
    ) -> AsyncIterator[tuple[ClientSession, Any]]:
        # A pool entry is only a handle: re-read the credential version before
        # EVERY session so another worker's rotation/disconnect takes effect.
        await self._load_oauth(force_version=self._oauth_recovery_version)
        self._oauth_recovery_version = None
        for attempt in range(2):
            yielded = False
            try:
                async with self._raw_session_context(
                    sse_read_timeout=sse_read_timeout
                ) as session:
                    yielded = True
                    yield session
                return
            except BaseException as error:
                cause = _unwrap_exception_group(error)
                if (
                    not yielded
                    and attempt == 0
                    and self.oauth_server_id is not None
                    and isinstance(cause, httpx.HTTPStatusError)
                    and cause.response.status_code == 401
                ):
                    await self._load_oauth(force_version=self.oauth_version)
                    continue
                raise

    async def connect(self):
        """Verify the MCP server is reachable.

        The MCP SDK's streamable HTTP client owns AnyIO task groups/cancel
        scopes. Keeping that async context open in a process-wide pool can
        close it from a different request task later, producing noisy async
        generator/cancel-scope RuntimeErrors. We only pool server metadata and
        use per-operation sessions that enter and exit in the same task.
        """
        try:
            async with self._session_context(sse_read_timeout=60.0) as (
                _session,
                init_result,
            ):
                pass
            self._connected = True
            self._session = None
            self._exit_stack = None
            logger.info(
                f"Connected to MCP server at {self.url} "
                f"(protocol: {init_result.protocol_version})"
            )
        except BaseException as e:
            target_exc = _unwrap_exception_group(e)
            logger.warning(
                "Failed to connect to MCP server: %s", describe_mcp_connection_error(e)
            )
            self._connected = False
            if isinstance(target_exc, Exception):
                if self.auth_type == "oauth":
                    raise PermissionError(
                        describe_mcp_connection_error(target_exc)
                    ) from None
                raise target_exc from None
            if "cancel scope" in str(target_exc).lower() or isinstance(
                e, BaseExceptionGroup
            ):
                raise ConnectionError(
                    "MCP connection aborted during session setup"
                ) from None
            raise

    async def close(self):
        """Close the connection to the MCP server."""
        self._exit_stack = None
        self._session = None
        was_connected = self._connected
        self._connected = False
        if was_connected:
            logger.info(f"Closed connection to MCP server at {self.url}")

    def is_connected(self) -> bool:
        """Check if client is connected.

        Returns:
            True if connected, False otherwise
        """
        return self._connected

    async def list_tools(self) -> List[types.Tool]:
        """List available tools from the MCP server.

        Returns:
            List of available tools

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Client not connected. Call connect() first.")

        try:
            async with self._session_context(sse_read_timeout=60.0) as (
                session,
                _init_result,
            ):
                result = await session.list_tools()
            return result.tools
        except BaseException as e:
            # NOTE: ExceptionGroup (py3.11) subclasses Exception, so a plain
            # `except Exception` would catch it first and re-raise verbatim as
            # "unhandled errors in a TaskGroup (N sub-exceptions)", hiding the
            # real cause (e.g. connection refused on an unreachable server).
            # Unwrap to the meaningful leaf, mirroring call_tool().
            target_exc = _unwrap_exception_group(e)

            logger.error(
                "Error listing MCP tools: %s", describe_mcp_connection_error(target_exc)
            )

            if isinstance(target_exc, Exception):
                if self.auth_type == "oauth":
                    raise PermissionError(
                        describe_mcp_connection_error(target_exc)
                    ) from None
                raise target_exc from None

            if "cancel scope" in str(target_exc).lower() or type(e).__name__ in (
                "ExceptionGroup",
                "BaseExceptionGroup",
            ):
                raise ConnectionError(
                    "MCP server is unavailable; check connection settings"
                ) from None

            raise e

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> UpstreamToolContent:
        """Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Content blocks, carrying the upstream ``is_error`` and
            ``structured_content``

        Raises:
            RuntimeError: If not connected
        """
        if not self._connected:
            raise RuntimeError("Client not connected. Call connect() first.")

        dispatched = False
        try:
            async with self._session_context(sse_read_timeout=60.0) as (
                session,
                _init_result,
            ):
                # Each operation owns a fresh session. Learn x-mcp-header
                # annotations before the one and only tools/call dispatch.
                if session.protocol_version in MODERN_PROTOCOL_VERSIONS:
                    cursor = None
                    seen: set[str] = set()
                    for _ in range(100):
                        page = await session.list_tools(
                            params=types.PaginatedRequestParams(cursor=cursor)
                        )
                        if any(tool.name == tool_name for tool in page.tools):
                            break
                        cursor = page.next_cursor
                        if cursor is None:
                            raise ValueError("MCP tool is not advertised by the server")
                        if cursor in seen:
                            raise ValueError(
                                "MCP server returned a repeated tools cursor"
                            )
                        seen.add(cursor)
                    else:
                        raise ValueError("MCP tool listing exceeded the page limit")
                dispatched = True
                # Execute tool call
                result = await session.call_tool(name=tool_name, arguments=arguments)

                # Convert to list and extract all data
                content_list = []
                for item in result.content:
                    if hasattr(item, "text"):
                        content_list.append(
                            types.TextContent(type="text", text=item.text)
                        )
                    elif hasattr(item, "data"):
                        content_list.append(
                            types.ImageContent(
                                type="image",
                                data=item.data,
                                mimeType=getattr(item, "mime_type", "image/png"),
                            )
                        )
                    elif hasattr(item, "resource"):
                        content_list.append(
                            types.EmbeddedResource(
                                type="resource", resource=item.resource
                            )
                        )

                structured = getattr(result, "structured_content", None)
                return UpstreamToolContent(
                    content_list,
                    is_error=getattr(result, "is_error", False) is True,
                    structured_content=(
                        structured if isinstance(structured, dict) else None
                    ),
                )
        except BaseException as e:
            # NOTE: ExceptionGroup (py3.11) subclasses Exception, so it would
            # otherwise be swallowed by a bare `except Exception` and re-raised
            # verbatim as "unhandled errors in a TaskGroup (N sub-exceptions)",
            # hiding the real cause (e.g. an upstream 502 → "Session
            # terminated"). Unwrap to the meaningful leaf so callers and logs
            # get an actionable message.
            target_exc = _unwrap_exception_group(e)

            if (
                isinstance(target_exc, httpx.HTTPStatusError)
                and target_exc.response.status_code == 401
            ):
                self._oauth_recovery_version = self.oauth_version
            if isinstance(target_exc, asyncio.CancelledError):
                raise
            message = describe_mcp_connection_error(target_exc)
            logger.warning("Upstream MCP tool request failed: %s", message)
            if dispatched and not (
                isinstance(target_exc, httpx.HTTPStatusError)
                and target_exc.response.status_code in {401, 403}
            ):
                raise MCPToolOutcomeUnknownError(
                    "The MCP tool request was dispatched but its outcome is unknown. "
                    "It may have executed. Check the remote result before deciding "
                    "whether to invoke it again. Preloop did not replay the request."
                ) from None
            if isinstance(target_exc, Exception):
                if self.auth_type == "oauth":
                    raise PermissionError(message) from None
                raise ConnectionError(message) from None
            if isinstance(e, BaseExceptionGroup):
                raise MCPToolOutcomeUnknownError(
                    "MCP tool connection ended; the remote outcome may be unknown. Check it before retrying."
                ) from None
            raise


class MCPClientPool:
    """Pool of MCP clients for external servers.

    Maintains persistent connections to user-configured external MCP servers
    and provides connection pooling and lifecycle management.
    """

    def __init__(self):
        """Initialize the client pool."""
        self._clients: Dict[str, MCPClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        logger.info("MCPClientPool initialized")

    def _get_lock(self, server_id: str) -> asyncio.Lock:
        """Get or create a lock for a specific server.

        Args:
            server_id: ID of the MCP server

        Returns:
            Lock for the server
        """
        if server_id not in self._locks:
            self._locks[server_id] = asyncio.Lock()
        return self._locks[server_id]

    async def get_client(
        self,
        server_id: str,
        url: str,
        auth_type: str = "none",
        auth_config: Optional[Dict[str, Any]] = None,
        transport: str = "http-streaming",
    ) -> MCPClient:
        """Get or create an MCP client for a server.

        Args:
            server_id: Unique ID of the MCP server
            url: Base URL of the MCP server
            auth_type: Authentication type
            auth_config: Authentication configuration
            transport: Transport protocol

        Returns:
            Connected MCP client

        Raises:
            Exception: If connection fails
        """
        fingerprint = mcp_client_config_fingerprint(
            url, auth_type, auth_config, transport
        )

        # Check if client already exists
        if server_id in self._clients:
            client = self._clients[server_id]
            if client.config_fingerprint != fingerprint:
                # The server row changed (new token, URL, auth type or
                # transport). Every caller reads the current row, so this
                # also picks up updates made on another pod.
                logger.info("MCP server config changed, rebuilding client")
                await self.close_client(server_id)
            elif client.is_connected():
                return client
            else:
                # Client exists but not connected, remove it
                # Avoid logging server_id: callers may pass ids from OAuth
                # state dicts that also hold client_secret (CodeQL taint).
                logger.warning("Existing MCP client not connected, recreating")
                await self.close_client(server_id)

        # Create new client with lock
        lock = self._get_lock(server_id)
        async with lock:
            # Double-check after acquiring lock
            existing = self._clients.get(server_id)
            if (
                existing is not None
                and existing.is_connected()
                and existing.config_fingerprint == fingerprint
            ):
                return existing
            if existing is not None:
                # Raced with another caller that cached a different config.
                await existing.close()
                del self._clients[server_id]

            # Create and connect new client
            client = MCPClient(
                url=url,
                auth_type=auth_type,
                auth_config=auth_config,
                transport=transport,
            )
            if auth_type == "oauth":
                client.oauth_server_id = UUID(server_id)
            await client.connect()
            self._clients[server_id] = client
            logger.info("Created new MCP client")

        return client

    async def close_client(self, server_id: str):
        """Close and remove a client from the pool.

        Args:
            server_id: ID of the MCP server
        """
        if server_id in self._clients:
            async with self._get_lock(server_id):
                if server_id in self._clients:
                    await self._clients[server_id].close()
                    del self._clients[server_id]
                    logger.info("Closed and removed MCP client")

    async def close_all(self):
        """Close all clients in the pool."""
        async with self._global_lock:
            for server_id in list(self._clients.keys()):
                await self.close_client(server_id)
            logger.info("Closed all MCP clients")

    def get_active_servers(self) -> List[str]:
        """Get list of server IDs with active connections.

        Returns:
            List of server IDs
        """
        return [
            server_id
            for server_id, client in self._clients.items()
            if client.is_connected()
        ]


# Global client pool instance
_client_pool: Optional[MCPClientPool] = None


def get_mcp_client_pool() -> MCPClientPool:
    """Get the global MCP client pool instance.

    Returns:
        Global MCPClientPool instance
    """
    global _client_pool
    if _client_pool is None:
        _client_pool = MCPClientPool()
    return _client_pool
