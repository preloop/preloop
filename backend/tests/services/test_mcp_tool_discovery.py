"""Tests for MCP tool discovery service."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services.mcp_tool_discovery import (
    get_all_enabled_proxied_tools,
    get_cached_tools_for_server,
    get_enabled_builtin_tools,
    scan_mcp_server_tools,
)
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool
from preloop.models.models.tool_configuration import ToolConfiguration

pytestmark = pytest.mark.asyncio


class TestScanMCPServerTools:
    """Test scan_mcp_server_tools function."""

    async def test_scan_new_tools_success(self, mocker):
        """Test scanning server with new tools."""
        # Setup mock database
        mock_db = MagicMock()
        server_id = uuid.uuid4()

        # Mock MCP server
        mock_server = MagicMock(spec=MCPServer)
        mock_server.id = server_id
        mock_server.name = "Test Server"
        mock_server.url = "http://test.com"
        mock_server.auth_type = "none"
        mock_server.auth_config = {}
        mock_server.transport = "http"

        # Mock database queries
        mock_db.query.return_value.filter.return_value.first.return_value = mock_server
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [],  # No existing tools
            [],  # Return empty for final query (will be replaced)
        ]

        # Mock MCP client
        mock_tool_obj = MagicMock()
        mock_tool_obj.name = "test_tool"
        mock_tool_obj.description = "Test tool description"
        mock_tool_obj.inputSchema = {"type": "object"}

        mock_client = AsyncMock()
        mock_client.list_tools.return_value = [mock_tool_obj]

        mock_client_pool = MagicMock()
        mock_client_pool.get_client = AsyncMock(return_value=mock_client)

        mocker.patch(
            "preloop.services.mcp_tool_discovery.get_mcp_client_pool",
            return_value=mock_client_pool,
        )

        # Track added tools
        added_tools = []

        def mock_add(obj):
            added_tools.append(obj)

        mock_db.add.side_effect = mock_add

        # Mock final query to return added tools
        def mock_query_side_effect(*args):
            mock_query = MagicMock()
            if args[0] == MCPServer:
                mock_query.filter.return_value.first.return_value = mock_server
            elif args[0] == MCPTool:
                # For the first call (checking existing), return empty
                # For the second call (getting all), return added tools
                if len(added_tools) > 0:
                    mock_query.filter.return_value.all.return_value = added_tools
                else:
                    mock_query.filter.return_value.all.return_value = []
            return mock_query

        mock_db.query.side_effect = mock_query_side_effect

        # Execute
        await scan_mcp_server_tools(server_id, mock_db)

        # Verify
        assert len(added_tools) == 1
        assert added_tools[0].name == "test_tool"
        assert added_tools[0].description == "Test tool description"
        assert mock_db.commit.called
        assert mock_server.status == "active"
        assert mock_server.last_error is None

    async def test_scan_update_existing_tools(self, mocker):
        """Test scanning server with existing tools to update."""
        # Setup mock database
        mock_db = MagicMock()
        server_id = uuid.uuid4()

        # Mock MCP server
        mock_server = MagicMock(spec=MCPServer)
        mock_server.id = server_id
        mock_server.name = "Test Server"
        mock_server.url = "http://test.com"
        mock_server.auth_type = "none"
        mock_server.auth_config = {}
        mock_server.transport = "http"

        # Mock existing tool
        existing_tool = MagicMock(spec=MCPTool)
        existing_tool.name = "test_tool"
        existing_tool.description = "Old description"
        existing_tool.input_schema = {"type": "object"}

        # Mock database queries
        def mock_query_side_effect(*args):
            mock_query = MagicMock()
            if args[0] == MCPServer:
                mock_query.filter.return_value.first.return_value = mock_server
            elif args[0] == MCPTool:
                mock_query.filter.return_value.all.return_value = [existing_tool]
            return mock_query

        mock_db.query.side_effect = mock_query_side_effect

        # Mock MCP client with updated tool
        mock_tool_obj = MagicMock()
        mock_tool_obj.name = "test_tool"
        mock_tool_obj.description = "Updated description"
        mock_tool_obj.inputSchema = {"type": "object", "properties": {}}

        mock_client = AsyncMock()
        mock_client.list_tools.return_value = [mock_tool_obj]

        mock_client_pool = MagicMock()
        mock_client_pool.get_client = AsyncMock(return_value=mock_client)

        mocker.patch(
            "preloop.services.mcp_tool_discovery.get_mcp_client_pool",
            return_value=mock_client_pool,
        )

        # Execute
        await scan_mcp_server_tools(server_id, mock_db)

        # Verify tool was updated
        assert existing_tool.description == "Updated description"
        assert mock_db.commit.called
        assert mock_server.status == "active"

    async def test_scan_server_not_found(self):
        """Test scanning non-existent server raises ValueError."""
        mock_db = MagicMock()
        server_id = uuid.uuid4()

        mock_db.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(ValueError) as exc_info:
            await scan_mcp_server_tools(server_id, mock_db)

        assert "MCP server not found" in str(exc_info.value)

    async def test_scan_client_error_returns_cached_tools_and_notifies(self, mocker):
        """Test scanning server when client raises error."""
        # Setup mock database
        mock_db = MagicMock()
        server_id = uuid.uuid4()

        # Mock MCP server
        mock_server = MagicMock(spec=MCPServer)
        mock_server.id = server_id
        mock_server.name = "Test Server"
        mock_server.url = "http://test.com"
        mock_server.auth_type = "none"
        mock_server.auth_config = {}
        mock_server.transport = "http"

        mock_db.query.return_value.filter.return_value.first.return_value = mock_server
        cached_tool = MagicMock(spec=MCPTool)
        cached_tool.name = "cached_tool"

        # Mock MCP client that raises error
        mock_client = AsyncMock()
        mock_client.list_tools.side_effect = Exception("Connection failed")

        mock_client_pool = MagicMock()
        mock_client_pool.get_client = AsyncMock(return_value=mock_client)

        mocker.patch(
            "preloop.services.mcp_tool_discovery.get_mcp_client_pool",
            return_value=mock_client_pool,
        )
        mock_notify = mocker.patch(
            "preloop.sync.tasks.notify_admins",
        )
        mocker.patch(
            "preloop.services.mcp_tool_discovery.crud_mcp_tool.get_by_server",
            return_value=[cached_tool],
        )

        # Execute
        result = await scan_mcp_server_tools(server_id, mock_db)

        assert result == [cached_tool]
        assert mock_server.status == "error"
        assert mock_server.last_error == "Connection failed"
        assert mock_db.commit.called
        mock_notify.assert_called_once()


class TestGetCachedToolsForServer:
    """Test get_cached_tools_for_server function."""

    async def test_get_cached_tools_success(self):
        """Test getting cached tools for a server."""
        mock_db = MagicMock()
        server_id = uuid.uuid4()

        # Mock tools
        tool1 = MagicMock(spec=MCPTool)
        tool1.name = "tool1"
        tool2 = MagicMock(spec=MCPTool)
        tool2.name = "tool2"

        mock_db.query.return_value.filter.return_value.all.return_value = [tool1, tool2]

        # Execute
        result = await get_cached_tools_for_server(server_id, mock_db)

        # Verify
        assert len(result) == 2
        assert result[0].name == "tool1"
        assert result[1].name == "tool2"

    async def test_get_cached_tools_empty(self):
        """Test getting cached tools when none exist."""
        mock_db = MagicMock()
        server_id = uuid.uuid4()

        mock_db.query.return_value.filter.return_value.all.return_value = []

        # Execute
        result = await get_cached_tools_for_server(server_id, mock_db)

        # Verify
        assert len(result) == 0
        assert result == []


class TestGetAllEnabledProxiedTools:
    """Test get_all_enabled_proxied_tools function."""

    async def test_get_enabled_proxied_tools_default_enabled(self):
        """Test getting proxied tools with default enabled state."""
        mock_db = MagicMock()
        account_id = str(uuid.uuid4())

        # Mock MCP server
        server = MagicMock(spec=MCPServer)
        server.id = uuid.uuid4()
        server.name = "Test Server"
        server.status = "active"

        # Mock tools
        tool1 = MagicMock(spec=MCPTool)
        tool1.name = "tool1"
        tool1.mcp_server_id = server.id

        tool2 = MagicMock(spec=MCPTool)
        tool2.name = "tool2"
        tool2.mcp_server_id = server.id

        # Mock database queries
        def mock_query_side_effect(*args):
            mock_query = MagicMock()
            if args[0] == MCPServer:
                mock_query.filter.return_value.all.return_value = [server]
            elif args[0] == ToolConfiguration:
                mock_query.filter.return_value.all.return_value = []  # No configs
            elif args[0] == MCPTool:
                mock_query.filter.return_value.all.return_value = [tool1, tool2]
            return mock_query

        mock_db.query.side_effect = mock_query_side_effect

        # Execute
        result = await get_all_enabled_proxied_tools(account_id, mock_db)

        # Verify - should include both tools as default is enabled
        assert len(result) == 2
        assert result[0] == (server, tool1)
        assert result[1] == (server, tool2)

    async def test_get_enabled_proxied_tools_with_disabled_tool(self):
        """Test getting proxied tools with one explicitly disabled."""
        mock_db = MagicMock()
        account_id = str(uuid.uuid4())

        # Mock MCP server
        server = MagicMock(spec=MCPServer)
        server.id = uuid.uuid4()
        server.name = "Test Server"
        server.status = "active"

        # Mock tools
        tool1 = MagicMock(spec=MCPTool)
        tool1.name = "tool1"
        tool1.mcp_server_id = server.id

        tool2 = MagicMock(spec=MCPTool)
        tool2.name = "tool2"
        tool2.mcp_server_id = server.id

        # Mock tool configuration (tool2 is disabled)
        config = MagicMock(spec=ToolConfiguration)
        config.tool_name = "tool2"
        config.mcp_server_id = server.id
        config.is_enabled = False

        # Mock database queries
        def mock_query_side_effect(*args):
            mock_query = MagicMock()
            if args[0] == MCPServer:
                mock_query.filter.return_value.all.return_value = [server]
            elif args[0] == ToolConfiguration:
                mock_query.filter.return_value.all.return_value = [config]
            elif args[0] == MCPTool:
                mock_query.filter.return_value.all.return_value = [tool1, tool2]
            return mock_query

        mock_db.query.side_effect = mock_query_side_effect

        # Execute
        result = await get_all_enabled_proxied_tools(account_id, mock_db)

        # Verify - should only include tool1
        assert len(result) == 1
        assert result[0] == (server, tool1)

    async def test_get_enabled_proxied_tools_no_servers(self):
        """Test getting proxied tools when no servers exist."""
        mock_db = MagicMock()
        account_id = str(uuid.uuid4())

        # Mock database queries
        def mock_query_side_effect(*args):
            mock_query = MagicMock()
            if args[0] == MCPServer:
                mock_query.filter.return_value.all.return_value = []
            elif args[0] == ToolConfiguration:
                mock_query.filter.return_value.all.return_value = []
            return mock_query

        mock_db.query.side_effect = mock_query_side_effect

        # Execute
        result = await get_all_enabled_proxied_tools(account_id, mock_db)

        # Verify
        assert len(result) == 0


class TestGetEnabledBuiltinTools:
    """Test get_enabled_builtin_tools function."""

    async def test_get_enabled_builtin_tools_default_enabled(self):
        """Test getting builtin tools with default enabled state."""
        mock_db = MagicMock()
        account_id = str(uuid.uuid4())

        # Mock builtin tools
        tool1 = MagicMock()
        tool1.name = "get_issue"
        tool2 = MagicMock()
        tool2.name = "search_issues"

        all_builtin_tools = [tool1, tool2]

        # Mock database query - no configurations
        mock_db.query.return_value.filter.return_value.all.return_value = []

        # Execute
        result = await get_enabled_builtin_tools(account_id, all_builtin_tools, mock_db)

        # Verify - should include all tools as default is enabled
        assert len(result) == 2
        assert result[0].name == "get_issue"
        assert result[1].name == "search_issues"

    async def test_get_enabled_builtin_tools_with_disabled_tool(self):
        """Test getting builtin tools with one explicitly disabled."""
        mock_db = MagicMock()
        account_id = str(uuid.uuid4())

        # Mock builtin tools
        tool1 = MagicMock()
        tool1.name = "get_issue"
        tool2 = MagicMock()
        tool2.name = "search_issues"

        all_builtin_tools = [tool1, tool2]

        # Mock tool configuration (search_issues is disabled)
        config = MagicMock(spec=ToolConfiguration)
        config.tool_name = "search_issues"
        config.is_enabled = False

        mock_db.query.return_value.filter.return_value.all.return_value = [config]

        # Execute
        result = await get_enabled_builtin_tools(account_id, all_builtin_tools, mock_db)

        # Verify - should only include get_issue
        assert len(result) == 1
        assert result[0].name == "get_issue"

    async def test_get_enabled_builtin_tools_all_disabled(self):
        """Test getting builtin tools when all are disabled."""
        mock_db = MagicMock()
        account_id = str(uuid.uuid4())

        # Mock builtin tools
        tool1 = MagicMock()
        tool1.name = "get_issue"
        tool2 = MagicMock()
        tool2.name = "search_issues"

        all_builtin_tools = [tool1, tool2]

        # Mock tool configurations (all disabled)
        config1 = MagicMock(spec=ToolConfiguration)
        config1.tool_name = "get_issue"
        config1.is_enabled = False

        config2 = MagicMock(spec=ToolConfiguration)
        config2.tool_name = "search_issues"
        config2.is_enabled = False

        mock_db.query.return_value.filter.return_value.all.return_value = [
            config1,
            config2,
        ]

        # Execute
        result = await get_enabled_builtin_tools(account_id, all_builtin_tools, mock_db)

        # Verify - should return empty list
        assert len(result) == 0

    async def test_get_enabled_builtin_tools_empty_input(self):
        """Test getting builtin tools with empty input list."""
        mock_db = MagicMock()
        account_id = str(uuid.uuid4())

        all_builtin_tools = []

        mock_db.query.return_value.filter.return_value.all.return_value = []

        # Execute
        result = await get_enabled_builtin_tools(account_id, all_builtin_tools, mock_db)

        # Verify
        assert len(result) == 0
