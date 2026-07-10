"""Tests for MCP Client Pool."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import types

from preloop.services.mcp_client_pool import (
    MCPClient,
    MCPClientPool,
    get_mcp_client_pool,
)

pytestmark = pytest.mark.asyncio


def mock_mcp_session():
    """Create mocks for a short-lived streamable HTTP MCP session."""
    mock_read_stream = MagicMock()
    mock_write_stream = MagicMock()
    mock_streams = AsyncMock()
    mock_streams.__aenter__ = AsyncMock(
        return_value=(mock_read_stream, mock_write_stream, None)
    )
    mock_streams.__aexit__ = AsyncMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_init_result = MagicMock()
    mock_init_result.protocolVersion = "2024-11-05"
    mock_init_result.serverInfo = MagicMock()
    mock_init_result.serverInfo.name = "test-server"
    mock_session.initialize = AsyncMock(return_value=mock_init_result)

    return mock_streams, mock_session


class TestMCPClientInit:
    """Test MCPClient initialization."""

    def test_init_with_defaults(self):
        """Test initialization with default values."""
        client = MCPClient(url="http://localhost:8001/mcp")

        assert client.url == "http://localhost:8001/mcp"
        assert client.auth_type == "none"
        assert client.auth_config == {}
        assert client.transport == "http-streaming"
        assert client._session is None
        assert client._connected is False

    def test_init_strips_trailing_slash(self):
        """Test that trailing slash is stripped from URL."""
        client = MCPClient(url="http://localhost:8001/mcp/")

        assert client.url == "http://localhost:8001/mcp"

    def test_init_with_bearer_auth(self):
        """Test initialization with bearer authentication."""
        client = MCPClient(
            url="http://localhost:8001/mcp",
            auth_type="bearer",
            auth_config={"token": "test-token"},
        )

        assert client.auth_type == "bearer"
        assert client.auth_config["token"] == "test-token"

    def test_init_with_api_key_auth(self):
        """Test initialization with API key authentication."""
        client = MCPClient(
            url="http://localhost:8001/mcp",
            auth_type="api_key",
            auth_config={"api_key": "test-key", "key_name": "X-Custom-Key"},
        )

        assert client.auth_type == "api_key"
        assert client.auth_config["api_key"] == "test-key"
        assert client.auth_config["key_name"] == "X-Custom-Key"


class TestMCPClientConnect:
    """Test MCPClient connection."""

    async def test_connect_success_no_auth(self):
        """Test successful connection without authentication."""
        client = MCPClient(url="http://localhost:8001/mcp")
        mock_streams, mock_session = mock_mcp_session()

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            await client.connect()

        assert client.is_connected()
        assert client._session is None

    async def test_connect_with_bearer_auth(self):
        """Test connection with bearer authentication."""
        client = MCPClient(
            url="http://localhost:8001/mcp",
            auth_type="bearer",
            auth_config={"token": "test-token"},
        )

        mock_streams, mock_session = mock_mcp_session()

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            await client.connect()

        assert client.is_connected()

    async def test_connect_with_api_key_auth(self):
        """Test connection with API key authentication."""
        client = MCPClient(
            url="http://localhost:8001/mcp",
            auth_type="api_key",
            auth_config={"api_key": "test-key"},
        )

        mock_streams, mock_session = mock_mcp_session()

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            await client.connect()

        assert client.is_connected()

    async def test_connect_failure_cleans_up(self):
        """Test that failed connection cleans up resources."""
        client = MCPClient(url="http://localhost:8001/mcp")

        mock_streams = AsyncMock()
        mock_streams.__aenter__ = AsyncMock(side_effect=Exception("Connection failed"))
        mock_streams.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "preloop.services.mcp_client_pool.streamablehttp_client",
            return_value=mock_streams,
        ):
            with pytest.raises(Exception, match="Connection failed"):
                await client.connect()

        assert not client.is_connected()

    async def test_connect_failure_does_not_leave_persistent_context(self):
        """Failed probes do not leave a cross-task context to close later."""
        client = MCPClient(url="http://localhost:8001/mcp")

        mock_streams = AsyncMock()
        mock_streams.__aenter__ = AsyncMock(side_effect=Exception("Connection failed"))
        mock_streams.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "preloop.services.mcp_client_pool.streamablehttp_client",
            return_value=mock_streams,
        ):
            with pytest.raises(Exception, match="Connection failed"):
                await client.connect()

        assert client._exit_stack is None
        assert client._session is None


class TestMCPClientClose:
    """Test MCPClient close."""

    async def test_close_connected_client(self):
        """Test closing a connected client."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True
        client._session = MagicMock()
        mock_exit_stack = AsyncMock()
        mock_exit_stack.aclose = AsyncMock()
        client._exit_stack = mock_exit_stack

        await client.close()

        assert not client.is_connected()
        assert client._session is None
        assert client._exit_stack is None

    async def test_close_when_not_connected(self):
        """Test closing when not connected."""
        client = MCPClient(url="http://localhost:8001/mcp")

        # Should not raise
        await client.close()

    async def test_close_clears_legacy_exit_stack_without_closing_cross_task_context(
        self,
    ):
        """Close only clears legacy pooled contexts to avoid cross-task aclose."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True
        old_exit_stack = AsyncMock()
        old_exit_stack.aclose = AsyncMock(
            side_effect=RuntimeError("cancel scope error")
        )
        client._exit_stack = old_exit_stack

        # Should not raise
        await client.close()

        assert not client.is_connected()
        old_exit_stack.aclose.assert_not_called()

    async def test_close_ignores_legacy_exit_stack_errors(self):
        """Legacy pooled contexts are abandoned instead of closed from this task."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True
        old_exit_stack = AsyncMock()
        old_exit_stack.aclose = AsyncMock(side_effect=RuntimeError("Different error"))
        client._exit_stack = old_exit_stack

        await client.close()

        assert not client.is_connected()
        old_exit_stack.aclose.assert_not_called()


class TestMCPClientListTools:
    """Test MCPClient list_tools."""

    async def test_list_tools_success(self):
        """Test successful tool listing."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True

        mock_tool1 = types.Tool(name="tool1", description="Tool 1", inputSchema={})
        mock_tool2 = types.Tool(name="tool2", description="Tool 2", inputSchema={})

        mock_result = MagicMock()
        mock_result.tools = [mock_tool1, mock_tool2]

        mock_streams, mock_session = mock_mcp_session()
        mock_session.list_tools = AsyncMock(return_value=mock_result)

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            tools = await client.list_tools()

        assert len(tools) == 2
        assert tools[0].name == "tool1"
        assert tools[1].name == "tool2"

    async def test_list_tools_not_connected(self):
        """Test that list_tools raises error when not connected."""
        client = MCPClient(url="http://localhost:8001/mcp")

        with pytest.raises(RuntimeError, match="not connected"):
            await client.list_tools()

    async def test_list_tools_error(self):
        """Test error handling in list_tools."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True

        mock_streams, mock_session = mock_mcp_session()
        mock_session.list_tools = AsyncMock(side_effect=Exception("List error"))

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            with pytest.raises(Exception, match="List error"):
                await client.list_tools()


class TestMCPClientCallTool:
    """Test MCPClient call_tool."""

    async def test_call_tool_success_text_content(self):
        """Test successful tool call with text content."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True
        client._session = MagicMock()

        # Mock the result from session.call_tool
        mock_result = MagicMock()
        mock_text_item = MagicMock()
        mock_text_item.text = "Result text"
        mock_result.content = [mock_text_item]

        # Mock streamablehttp_client context manager
        mock_read_stream = MagicMock()
        mock_write_stream = MagicMock()
        mock_streams = AsyncMock()
        mock_streams.__aenter__ = AsyncMock(
            return_value=(mock_read_stream, mock_write_stream, None)
        )
        mock_streams.__aexit__ = AsyncMock(return_value=None)

        # Mock ClientSession
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            result = await client.call_tool("test_tool", {"arg": "value"})

        assert len(result) == 1
        assert isinstance(result[0], types.TextContent)
        assert result[0].text == "Result text"

    async def test_call_tool_unwraps_exception_group_to_leaf(self):
        """A TaskGroup ExceptionGroup must surface its meaningful leaf cause.

        Regression: an upstream outage (e.g. 502 → ``Session terminated``) is
        raised by the MCP SDK wrapped in one/more ``ExceptionGroup`` layers.
        call_tool must raise the leaf (actionable) rather than the opaque
        "unhandled errors in a TaskGroup" wrapper.
        """
        from preloop.services.mcp_client_pool import _unwrap_exception_group

        leaf = RuntimeError("Session terminated")
        nested = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ExceptionGroup("inner", [leaf])],
        )
        assert _unwrap_exception_group(nested) is leaf

        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True

        mock_streams = AsyncMock()
        mock_streams.__aenter__ = AsyncMock(
            return_value=(MagicMock(), MagicMock(), None)
        )
        mock_streams.__aexit__ = AsyncMock(return_value=None)
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=nested)

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            with pytest.raises(RuntimeError, match="Session terminated"):
                await client.call_tool("pay", {"amount": 500})

    async def test_call_tool_success_image_content(self):
        """Test successful tool call with image content."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True
        client._session = MagicMock()

        # Mock the result from session.call_tool
        mock_result = MagicMock()
        # Create mock with spec to control hasattr
        mock_image_item = MagicMock(spec=["data", "mimeType"])
        mock_image_item.data = b"image_data"
        mock_image_item.mimeType = "image/png"
        mock_result.content = [mock_image_item]

        # Mock streamablehttp_client context manager
        mock_read_stream = MagicMock()
        mock_write_stream = MagicMock()
        mock_streams = AsyncMock()
        mock_streams.__aenter__ = AsyncMock(
            return_value=(mock_read_stream, mock_write_stream, None)
        )
        mock_streams.__aexit__ = AsyncMock(return_value=None)

        # Mock ClientSession
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            result = await client.call_tool("test_tool", {})

        assert len(result) == 1
        assert isinstance(result[0], types.ImageContent)
        # types.ImageContent converts bytes to string
        assert result[0].data in [b"image_data", "image_data"]

    async def test_call_tool_success_multiple_content_types(self):
        """Test successful tool call with multiple content types."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True
        client._session = MagicMock()

        # Mock the result from session.call_tool
        mock_result = MagicMock()
        # Mix of text and image content
        mock_text_item = MagicMock()
        mock_text_item.text = "Text result"
        mock_image_item = MagicMock(spec=["data", "mimeType"])
        mock_image_item.data = b"image_data"
        mock_image_item.mimeType = "image/png"
        mock_result.content = [mock_text_item, mock_image_item]

        # Mock streamablehttp_client context manager
        mock_read_stream = MagicMock()
        mock_write_stream = MagicMock()
        mock_streams = AsyncMock()
        mock_streams.__aenter__ = AsyncMock(
            return_value=(mock_read_stream, mock_write_stream, None)
        )
        mock_streams.__aexit__ = AsyncMock(return_value=None)

        # Mock ClientSession
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            result = await client.call_tool("test_tool", {})

        assert len(result) == 2
        assert isinstance(result[0], types.TextContent)
        assert isinstance(result[1], types.ImageContent)

    async def test_call_tool_not_connected(self):
        """Test that call_tool raises error when not connected."""
        client = MCPClient(url="http://localhost:8001/mcp")

        with pytest.raises(RuntimeError, match="not connected"):
            await client.call_tool("test_tool", {})

    async def test_call_tool_cleanup_properly_ordered(self):
        """Test that AsyncExitStack ensures proper cleanup ordering."""
        client = MCPClient(url="http://localhost:8001/mcp")
        client._connected = True
        client._session = MagicMock()

        # Track cleanup order
        cleanup_order = []

        # Mock the result from session.call_tool
        mock_result = MagicMock()
        mock_text_item = MagicMock()
        mock_text_item.text = "Result"
        mock_result.content = [mock_text_item]

        # Mock streamablehttp_client context manager
        mock_read_stream = MagicMock()
        mock_write_stream = MagicMock()
        mock_streams = AsyncMock()
        mock_streams.__aenter__ = AsyncMock(
            return_value=(mock_read_stream, mock_write_stream, None)
        )

        async def streams_cleanup(*args):
            cleanup_order.append("streams")
            return None

        mock_streams.__aexit__ = AsyncMock(side_effect=streams_cleanup)

        # Mock ClientSession
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)

        async def session_cleanup(*args):
            cleanup_order.append("session")
            return None

        mock_session.__aexit__ = AsyncMock(side_effect=session_cleanup)
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        with (
            patch(
                "preloop.services.mcp_client_pool.streamablehttp_client",
                return_value=mock_streams,
            ),
            patch(
                "preloop.services.mcp_client_pool.ClientSession",
                return_value=mock_session,
            ),
        ):
            result = await client.call_tool("test_tool", {})

        assert len(result) == 1
        # AsyncExitStack ensures LIFO cleanup: session exits before streams
        assert cleanup_order == ["session", "streams"]


class TestMCPClientPoolInit:
    """Test MCPClientPool initialization."""

    def test_init_creates_empty_pool(self):
        """Test initialization creates empty pool."""
        pool = MCPClientPool()

        assert pool._clients == {}
        assert pool._locks == {}
        assert pool._global_lock is not None


class TestMCPClientPoolGetLock:
    """Test MCPClientPool _get_lock."""

    def test_get_lock_creates_new_lock(self):
        """Test that _get_lock creates lock for new server."""
        pool = MCPClientPool()

        lock1 = pool._get_lock("server1")

        assert lock1 is not None
        assert "server1" in pool._locks

    def test_get_lock_returns_existing_lock(self):
        """Test that _get_lock returns existing lock."""
        pool = MCPClientPool()

        lock1 = pool._get_lock("server1")
        lock2 = pool._get_lock("server1")

        assert lock1 is lock2


class TestMCPClientPoolGetClient:
    """Test MCPClientPool get_client."""

    async def test_get_client_creates_new_client(self):
        """Test creating new client."""
        pool = MCPClientPool()

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        mock_client.connect = AsyncMock()

        with patch(
            "preloop.services.mcp_client_pool.MCPClient", return_value=mock_client
        ):
            client = await pool.get_client(
                server_id="server1",
                url="http://localhost:8001/mcp",
            )

        assert client == mock_client
        assert "server1" in pool._clients
        mock_client.connect.assert_called_once()

    async def test_get_client_returns_existing_connected_client(self):
        """Test returning existing connected client."""
        pool = MCPClientPool()

        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)
        pool._clients["server1"] = mock_client

        client = await pool.get_client(
            server_id="server1",
            url="http://localhost:8001/mcp",
        )

        assert client == mock_client

    async def test_get_client_recreates_disconnected_client(self):
        """Test recreating disconnected client."""
        pool = MCPClientPool()

        old_client = AsyncMock()
        old_client.is_connected = MagicMock(return_value=False)
        old_client.close = AsyncMock()
        pool._clients["server1"] = old_client

        new_client = AsyncMock()
        new_client.is_connected = MagicMock(return_value=True)
        new_client.connect = AsyncMock()

        with patch(
            "preloop.services.mcp_client_pool.MCPClient", return_value=new_client
        ):
            client = await pool.get_client(
                server_id="server1",
                url="http://localhost:8001/mcp",
            )

        assert client == new_client
        old_client.close.assert_called_once()

    async def test_get_client_with_auth_config(self):
        """Test creating client with authentication."""
        pool = MCPClientPool()

        mock_client_instance = AsyncMock()
        mock_client_instance.is_connected = MagicMock(return_value=True)
        mock_client_instance.connect = AsyncMock()

        with patch("preloop.services.mcp_client_pool.MCPClient") as mock_client_class:
            mock_client_class.return_value = mock_client_instance

            await pool.get_client(
                server_id="server1",
                url="http://localhost:8001/mcp",
                auth_type="bearer",
                auth_config={"token": "test-token"},
            )

            mock_client_class.assert_called_once_with(
                url="http://localhost:8001/mcp",
                auth_type="bearer",
                auth_config={"token": "test-token"},
                transport="http-streaming",
            )


class TestMCPClientPoolCloseClient:
    """Test MCPClientPool close_client."""

    async def test_close_client_removes_client(self):
        """Test closing and removing client."""
        pool = MCPClientPool()

        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        pool._clients["server1"] = mock_client

        await pool.close_client("server1")

        assert "server1" not in pool._clients
        mock_client.close.assert_called_once()

    async def test_close_client_nonexistent_server(self):
        """Test closing nonexistent client."""
        pool = MCPClientPool()

        # Should not raise
        await pool.close_client("nonexistent")


class TestMCPClientPoolCloseAll:
    """Test MCPClientPool close_all."""

    async def test_close_all_closes_all_clients(self):
        """Test closing all clients."""
        pool = MCPClientPool()

        mock_client1 = AsyncMock()
        mock_client1.close = AsyncMock()
        mock_client2 = AsyncMock()
        mock_client2.close = AsyncMock()

        pool._clients["server1"] = mock_client1
        pool._clients["server2"] = mock_client2

        await pool.close_all()

        assert len(pool._clients) == 0
        mock_client1.close.assert_called_once()
        mock_client2.close.assert_called_once()


class TestMCPClientPoolGetActiveServers:
    """Test MCPClientPool get_active_servers."""

    def test_get_active_servers_returns_connected(self):
        """Test returning only connected servers."""
        pool = MCPClientPool()

        mock_client1 = MagicMock()
        mock_client1.is_connected = MagicMock(return_value=True)
        mock_client2 = MagicMock()
        mock_client2.is_connected = MagicMock(return_value=False)
        mock_client3 = MagicMock()
        mock_client3.is_connected = MagicMock(return_value=True)

        pool._clients["server1"] = mock_client1
        pool._clients["server2"] = mock_client2
        pool._clients["server3"] = mock_client3

        active = pool.get_active_servers()

        assert "server1" in active
        assert "server2" not in active
        assert "server3" in active

    def test_get_active_servers_empty_pool(self):
        """Test with empty pool."""
        pool = MCPClientPool()

        active = pool.get_active_servers()

        assert active == []


class TestGetMCPClientPool:
    """Test get_mcp_client_pool function."""

    def test_get_mcp_client_pool_creates_singleton(self):
        """Test that get_mcp_client_pool creates singleton."""
        # Reset global
        import preloop.services.mcp_client_pool as module

        module._client_pool = None

        pool1 = get_mcp_client_pool()
        pool2 = get_mcp_client_pool()

        assert pool1 is pool2
        assert isinstance(pool1, MCPClientPool)


class TestIsMcpUnavailableError:
    """Transport-failure classification for friendly agent retries."""

    def test_typed_connection_and_timeout_errors(self):
        from preloop.services.mcp_client_pool import is_mcp_unavailable_error

        assert is_mcp_unavailable_error(ConnectionError("boom"))
        assert is_mcp_unavailable_error(TimeoutError("boom"))

    def test_transport_message_markers(self):
        from preloop.services.mcp_client_pool import is_mcp_unavailable_error

        assert is_mcp_unavailable_error(RuntimeError("Session terminated"))
        assert is_mcp_unavailable_error(RuntimeError("Connection refused"))
        assert is_mcp_unavailable_error(RuntimeError("All connection attempts failed"))

    def test_tool_level_messages_are_not_false_positives(self):
        from preloop.services.mcp_client_pool import is_mcp_unavailable_error

        # Bare "connection" / "unavailable" used to match; tool errors that
        # merely mention those words must stay classified as tool failures.
        assert not is_mcp_unavailable_error(
            RuntimeError("Database connection pool exhausted for query")
        )
        assert not is_mcp_unavailable_error(
            RuntimeError("Feature unavailable for this plan")
        )
        assert not is_mcp_unavailable_error(RuntimeError("invalid amount: 502"))
