"""
Direct MCP client for integration testing.

This module provides a fast, reliable way to test MCP endpoints without
spawning Claude CLI processes. Uses the Python MCP client library to
connect directly to Preloop's MCP HTTP endpoint.
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.integration.mcp_http_client import create_streamable_mcp_http_client


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Return the first meaningful leaf from a nested exception group."""
    if type(exc).__name__ in ("ExceptionGroup", "BaseExceptionGroup"):
        for sub in getattr(exc, "exceptions", []) or []:
            leaf = _unwrap_exception_group(sub)
            if isinstance(leaf, Exception):
                return leaf
        for sub in getattr(exc, "exceptions", []) or []:
            return _unwrap_exception_group(sub)
    return exc


def _is_misdirected_request(exc: BaseException) -> bool:
    """Return True when an exception chain includes HTTP 421."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 421
    if type(exc).__name__ in ("ExceptionGroup", "BaseExceptionGroup"):
        return any(_is_misdirected_request(sub) for sub in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        return _is_misdirected_request(cause)
    return False


def _is_transient_gateway_error(exc: BaseException) -> bool:
    """Return True when an exception chain includes a retryable gateway status."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {502, 503, 504}
    if type(exc).__name__ in ("ExceptionGroup", "BaseExceptionGroup"):
        return any(_is_transient_gateway_error(sub) for sub in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        return _is_transient_gateway_error(cause)
    return False


def _should_retry_mcp_connect(exc: BaseException) -> bool:
    """Return True for transient transport failures during MCP initialize."""
    if _is_misdirected_request(exc) or _is_transient_gateway_error(exc):
        return True
    if isinstance(exc, asyncio.CancelledError):
        return True
    leaf = _unwrap_exception_group(exc)
    if isinstance(leaf, httpx.HTTPError):
        return True
    return False


class MCPTestClient:
    """Test client for MCP endpoints."""

    def __init__(self, base_url: str, api_key: str):
        """
        Initialize MCP test client.

        Args:
            base_url: Preloop base URL (e.g., https://test.preloop.ai)
            api_key: API key for authentication
        """
        self.base_url = base_url.rstrip("/")
        self.mcp_url = f"{self.base_url}/mcp/v1"
        self.api_key = api_key
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def _close_stack(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc_val, exc_tb)
        self._stack = None
        self.session = None

    async def _open_session(self) -> None:
        """Open a Streamable HTTP MCP session against the deployed instance."""
        headers = {"Authorization": f"Bearer {self.api_key}"}

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        read_stream, write_stream, _ = await self._stack.enter_async_context(
            streamablehttp_client(
                url=self.mcp_url,
                headers=headers,
                httpx_client_factory=create_streamable_mcp_http_client,
            )
        )

        self.session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self.session.initialize()

    async def __aenter__(self):
        """Async context manager entry."""
        last_error: BaseException | None = None
        for attempt in range(5):
            try:
                await self._open_session()
                return self
            except BaseException as exc:
                # Catch ExceptionGroup/CancelledError from MCP transport setup; retry below.
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                last_error = exc
                await self._close_stack(type(exc), exc, exc.__traceback__)
                if attempt < 4 and _should_retry_mcp_connect(exc):
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise _unwrap_exception_group(exc) from exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to open MCP session")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self._close_stack(exc_type, exc_val, exc_tb)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Call an MCP tool.

        Args:
            tool_name: Name of the tool (e.g., "create_issue", "search")
            arguments: Tool arguments as a dictionary

        Returns:
            Tool response
        """
        if not self.session:
            raise RuntimeError("Client not connected. Call connect() first.")

        result = await self.session.call_tool(tool_name, arguments)
        return result

    async def create_issue(
        self,
        project: str,
        title: str,
        description: str,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        assignee: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create an issue via MCP.

        Args:
            project: Project identifier (e.g., "owner/repo" for GitHub)
            title: Issue title
            description: Issue description
            status: Optional issue status
            priority: Optional issue priority
            assignee: Optional assignee
            labels: Optional list of labels

        Returns:
            Created issue data
        """
        arguments = {
            "project": project,
            "title": title,
            "description": description,
        }
        if status:
            arguments["status"] = status
        if priority:
            arguments["priority"] = priority
        if assignee:
            arguments["assignee"] = assignee
        if labels:
            arguments["labels"] = labels

        return await self.call_tool("create_issue", arguments)

    async def get_issue(self, issue: str) -> Dict[str, Any]:
        """
        Get issue details via MCP.

        Args:
            issue: Issue identifier (e.g., "owner/repo#123")

        Returns:
            Issue data
        """
        return await self.call_tool("get_issue", {"issue": issue})

    async def update_issue(
        self,
        issue: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        assignee: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Update an issue via MCP.

        Args:
            issue: Issue identifier (e.g., "owner/repo#123")
            title: Optional new title
            description: Optional new description
            status: Optional new status
            priority: Optional new priority
            assignee: Optional new assignee
            labels: Optional new labels

        Returns:
            Updated issue data
        """
        arguments = {"issue": issue}
        if title:
            arguments["title"] = title
        if description:
            arguments["description"] = description
        if status:
            arguments["status"] = status
        if priority:
            arguments["priority"] = priority
        if assignee:
            arguments["assignee"] = assignee
        if labels:
            arguments["labels"] = labels

        return await self.call_tool("update_issue", arguments)

    async def search(
        self,
        query: str,
        project: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """
        Search for issues via MCP.

        Args:
            query: Search query
            project: Optional project filter
            limit: Maximum number of results

        Returns:
            Search results
        """
        arguments = {"query": query, "limit": limit}
        if project:
            arguments["project"] = project

        return await self.call_tool("search", arguments)


def run_async_test(coro):
    """
    Helper to run async test code synchronously.

    Args:
        coro: Coroutine to run

    Returns:
        Coroutine result
    """
    return asyncio.run(coro)
