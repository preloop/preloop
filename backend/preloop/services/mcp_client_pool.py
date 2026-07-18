"""MCP Client Pool for managing connections to external MCP servers.

This module provides connection pooling and management for external MCP servers.
It maintains persistent HTTP connections and handles authentication.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from preloop.services.mcp_httpx_client import create_streamable_mcp_http_client

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
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
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
    async def _session_context(self, *, sse_read_timeout: float = 60.0):
        """Create a short-lived MCP session in the current task."""
        headers, auth = self._build_auth()

        async with streamablehttp_client(
            url=self.url,
            headers=headers if not auth else {},  # Use auth param if bearer
            timeout=30.0,
            sse_read_timeout=sse_read_timeout,
            auth=auth,
            httpx_client_factory=create_streamable_mcp_http_client,
        ) as (read_stream, write_stream, _):
            session = ClientSession(
                read_stream=read_stream,
                write_stream=write_stream,
            )
            async with session:
                init_result = await session.initialize()
                yield session, init_result

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
                f"(protocol: {init_result.protocolVersion}, "
                f"server: {init_result.serverInfo.name})"
            )
        except Exception as e:
            logger.warning("Failed to connect to MCP server at %s: %s", self.url, e)
            self._connected = False
            raise
        except BaseException as e:
            # Catch BaseExceptionGroup which can be triggered when a TaskGroup
            # receives a GeneratorExit alongside a real Exception (like HTTPStatusError)
            target_exc = e
            if type(e).__name__ in ("ExceptionGroup", "BaseExceptionGroup"):
                # Extract the first normal Exception (e.g. HTTPStatusError)
                # ignoring BaseExceptions like GeneratorExit
                exceptions = getattr(e, "exceptions", [])
                for exc in exceptions:
                    if isinstance(exc, Exception):
                        target_exc = exc
                        break

            logger.warning(
                "Failed to connect to MCP server at %s: %s",
                self.url,
                target_exc,
            )
            self._connected = False

            # Only re-raise as Exception if we unwrapped an Exception.
            # If it's pure BaseException (like KeyboardInterrupt), re-raise it perfectly.
            if isinstance(target_exc, Exception):
                raise target_exc

            # Convert SDK cancel scope BaseExceptions into standard Exceptions to prevent router 500s
            if "cancel scope" in str(target_exc).lower() or type(e).__name__ in (
                "ExceptionGroup",
                "BaseExceptionGroup",
            ):
                raise ConnectionError(f"Connection aborted: {target_exc}")

            raise e

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
        except Exception as e:
            logger.error(f"Error listing tools: {e}", exc_info=True)
            raise
        except BaseException as e:
            target_exc = e
            if type(e).__name__ in ("ExceptionGroup", "BaseExceptionGroup"):
                exceptions = getattr(e, "exceptions", [])
                for exc in exceptions:
                    if isinstance(exc, Exception):
                        target_exc = exc
                        break

            logger.error(f"Error listing tools: {target_exc}")

            if isinstance(target_exc, Exception):
                raise target_exc

            if "cancel scope" in str(target_exc).lower() or type(e).__name__ in (
                "ExceptionGroup",
                "BaseExceptionGroup",
            ):
                raise ConnectionError(f"Operation aborted: {target_exc}")

            raise e

    async def call_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> List[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool execution result

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
                                mimeType=getattr(item, "mimeType", "image/png"),
                            )
                        )
                    elif hasattr(item, "resource"):
                        content_list.append(
                            types.EmbeddedResource(
                                type="resource", resource=item.resource
                            )
                        )

                return content_list
        except BaseException as e:
            # NOTE: ExceptionGroup (py3.11) subclasses Exception, so it would
            # otherwise be swallowed by a bare `except Exception` and re-raised
            # verbatim as "unhandled errors in a TaskGroup (N sub-exceptions)",
            # hiding the real cause (e.g. an upstream 502 → "Session
            # terminated"). Unwrap to the meaningful leaf so callers and logs
            # get an actionable message.
            target_exc = _unwrap_exception_group(e)

            if isinstance(target_exc, Exception):
                logger.error(
                    "Error calling tool %s on %s: %s",
                    tool_name,
                    self.url,
                    target_exc,
                )
                raise target_exc from None

            # Pure BaseException (GeneratorExit/cancel scope, etc.): convert the
            # noisy async-teardown case into a clean ConnectionError, otherwise
            # re-raise untouched.
            logger.error(
                "Error calling tool %s on %s: %s", tool_name, self.url, target_exc
            )
            if "cancel scope" in str(target_exc).lower() or type(e).__name__ in (
                "ExceptionGroup",
                "BaseExceptionGroup",
            ):
                raise ConnectionError(
                    f"MCP server '{self.url}' is unavailable: {target_exc}"
                ) from None
            raise e


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
        # Check if client already exists
        if server_id in self._clients:
            client = self._clients[server_id]
            if client.is_connected():
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
            if server_id in self._clients and self._clients[server_id].is_connected():
                return self._clients[server_id]

            # Create and connect new client
            client = MCPClient(
                url=url,
                auth_type=auth_type,
                auth_config=auth_config,
                transport=transport,
            )
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
