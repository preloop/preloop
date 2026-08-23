"""Tests for MCP config service."""

import json
from unittest.mock import patch

from preloop.services.mcp_config_service import MCPConfigService


class TestGenerateMCPConfig:
    """Test generate_mcp_config method."""

    def test_generate_mcp_config_with_preloop_mcp(self):
        """Test generating config with preloop-mcp server."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[
                {"server_name": "preloop-mcp", "tool_name": "get_issue"},
                {"server_name": "preloop-mcp", "tool_name": "search_issues"},
            ],
            preloop_url="https://app.test.com",
            account_api_token="test_token_123",
        )

        assert "mcpServers" in config
        assert "preloop-mcp" in config["mcpServers"]

        server_config = config["mcpServers"]["preloop-mcp"]
        assert server_config["url"] == "https://app.test.com/mcp/v1"
        assert server_config["transport"] == "http-streaming"
        assert "headers" in server_config
        assert "Authorization" in server_config["headers"]
        assert server_config["headers"]["Authorization"] == "Bearer test_token_123"

        # Check allowed tools
        assert "allowed_tools" in config
        assert "preloop-mcp" in config["allowed_tools"]
        assert "get_issue" in config["allowed_tools"]["preloop-mcp"]
        assert "search_issues" in config["allowed_tools"]["preloop-mcp"]

    def test_generate_mcp_config_without_token(self):
        """Test generating config without API token."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[],
            preloop_url="https://app.test.com",
        )

        server_config = config["mcpServers"]["preloop-mcp"]
        assert "headers" not in server_config

    @patch("preloop.services.mcp_config_service.os.getenv")
    def test_generate_mcp_config_default_url(self, mock_getenv):
        """Test generating config with default URL from environment."""
        mock_getenv.return_value = "http://localhost:8000"

        MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[],
        )

        mock_getenv.assert_called_with(
            "PRELOOP_URL", "http://host.docker.internal:8000"
        )

    def test_generate_mcp_config_unknown_server_excluded(self):
        """Test that unknown MCP server is excluded from config."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["unknown-server"],
            allowed_mcp_tools=[],
            preloop_url="https://app.test.com",
        )

        # Unknown server should not be in config
        assert "unknown-server" not in config["mcpServers"]
        # Config should still be valid
        assert "mcpServers" in config
        assert "allowed_tools" in config

    def test_generate_mcp_config_empty_servers(self):
        """Test generating config with no servers."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            preloop_url="https://app.test.com",
        )

        assert config["mcpServers"] == {}
        assert config["allowed_tools"] == {}

    def test_generate_mcp_config_multiple_tools_same_server(self):
        """Test generating config with multiple tools for same server."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[
                {"server_name": "preloop-mcp", "tool_name": "tool1"},
                {"server_name": "preloop-mcp", "tool_name": "tool2"},
                {"server_name": "preloop-mcp", "tool_name": "tool3"},
            ],
            preloop_url="https://app.test.com",
        )

        tools = config["allowed_tools"]["preloop-mcp"]
        assert len(tools) == 3
        assert "tool1" in tools
        assert "tool2" in tools
        assert "tool3" in tools

    def test_generate_mcp_config_tool_without_server_name(self):
        """Test that tools without server_name are skipped."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[
                {"tool_name": "tool1"},  # Missing server_name
                {"server_name": "preloop-mcp", "tool_name": "tool2"},
            ],
            preloop_url="https://app.test.com",
        )

        # Only tool2 should be included
        tools = config["allowed_tools"]["preloop-mcp"]
        assert len(tools) == 1
        assert "tool2" in tools

    def test_generate_mcp_config_unknown_repo_audit_is_skipped(self):
        """repo-audit is not a second MCP product; unknown servers are skipped."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["repo-audit"],
            allowed_mcp_tools=[],
            preloop_url="https://app.test.com",
        )
        assert "repo-audit" not in config["mcpServers"]
        assert config["mcpServers"] == {}

    def test_generate_mcp_config_tool_without_tool_name(self):
        """Test that tools without tool_name are skipped."""
        config = MCPConfigService.generate_mcp_config(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[
                {"server_name": "preloop-mcp"},  # Missing tool_name
                {"server_name": "preloop-mcp", "tool_name": "tool2"},
            ],
            preloop_url="https://app.test.com",
        )

        # Only tool2 should be included
        tools = config["allowed_tools"]["preloop-mcp"]
        assert len(tools) == 1
        assert "tool2" in tools


class TestGenerateMCPEnvironmentVars:
    """Test generate_mcp_environment_vars method."""

    def test_generate_mcp_environment_vars_with_servers_and_tools(self):
        """Test generating environment variables with servers and tools."""
        env = MCPConfigService.generate_mcp_environment_vars(
            allowed_mcp_servers=["preloop-mcp", "other-server"],
            allowed_mcp_tools=[
                {"server_name": "preloop-mcp", "tool_name": "get_issue"},
                {"server_name": "preloop-mcp", "tool_name": "search_issues"},
                {"server_name": "other-server", "tool_name": "other_tool"},
            ],
        )

        # Check MCP_ALLOWED_SERVERS
        assert "MCP_ALLOWED_SERVERS" in env
        assert "preloop-mcp" in env["MCP_ALLOWED_SERVERS"]
        assert "other-server" in env["MCP_ALLOWED_SERVERS"]

        # Check MCP_ALLOWED_TOOLS
        assert "MCP_ALLOWED_TOOLS" in env
        tools_map = json.loads(env["MCP_ALLOWED_TOOLS"])
        assert "preloop-mcp" in tools_map
        assert "get_issue" in tools_map["preloop-mcp"]
        assert "search_issues" in tools_map["preloop-mcp"]
        assert "other-server" in tools_map
        assert "other_tool" in tools_map["other-server"]

        # Check PRELOOP_MCP_URL
        assert "PRELOOP_MCP_URL" in env
        assert "/mcp/v1" in env["PRELOOP_MCP_URL"]

    def test_generate_mcp_environment_vars_empty_servers(self):
        """Test generating environment variables with no servers."""
        env = MCPConfigService.generate_mcp_environment_vars(
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
        )

        assert "MCP_ALLOWED_SERVERS" not in env
        assert "MCP_ALLOWED_TOOLS" not in env
        assert "PRELOOP_MCP_URL" in env  # Always included

    def test_generate_mcp_environment_vars_tool_without_server(self):
        """Test that tools without server_name are skipped."""
        env = MCPConfigService.generate_mcp_environment_vars(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[
                {"tool_name": "tool1"},  # Missing server_name
                {"server_name": "preloop-mcp", "tool_name": "tool2"},
            ],
        )

        tools_map = json.loads(env["MCP_ALLOWED_TOOLS"])
        # Only tool2 should be included
        assert len(tools_map["preloop-mcp"]) == 1
        assert "tool2" in tools_map["preloop-mcp"]

    @patch("preloop.services.mcp_config_service.os.getenv")
    def test_generate_mcp_environment_vars_custom_url(self, mock_getenv):
        """Test generating environment variables with custom URL."""
        mock_getenv.return_value = "https://custom.com"

        env = MCPConfigService.generate_mcp_environment_vars(
            allowed_mcp_servers=["preloop-mcp"],
            allowed_mcp_tools=[],
        )

        assert env["PRELOOP_MCP_URL"] == "https://custom.com/mcp/v1"


class TestValidateToolAccess:
    """Test validate_tool_access method."""

    def test_validate_tool_access_allowed(self):
        """Test validating access to an allowed tool."""
        allowed_tools = [
            {"server_name": "preloop-mcp", "tool_name": "get_issue"},
            {"server_name": "preloop-mcp", "tool_name": "search_issues"},
        ]

        result = MCPConfigService.validate_tool_access(
            server_name="preloop-mcp",
            tool_name="get_issue",
            allowed_mcp_tools=allowed_tools,
        )

        assert result is True

    def test_validate_tool_access_denied(self):
        """Test validating access to a non-allowed tool."""
        allowed_tools = [
            {"server_name": "preloop-mcp", "tool_name": "get_issue"},
        ]

        result = MCPConfigService.validate_tool_access(
            server_name="preloop-mcp",
            tool_name="create_issue",
            allowed_mcp_tools=allowed_tools,
        )

        assert result is False

    def test_validate_tool_access_wrong_server(self):
        """Test validating access with wrong server name."""
        allowed_tools = [
            {"server_name": "preloop-mcp", "tool_name": "get_issue"},
        ]

        result = MCPConfigService.validate_tool_access(
            server_name="other-server",
            tool_name="get_issue",
            allowed_mcp_tools=allowed_tools,
        )

        assert result is False

    def test_validate_tool_access_empty_list(self):
        """Test validating access with empty allowed tools list."""
        result = MCPConfigService.validate_tool_access(
            server_name="preloop-mcp",
            tool_name="get_issue",
            allowed_mcp_tools=[],
        )

        assert result is False

    def test_validate_tool_access_multiple_servers(self):
        """Test validating access with multiple servers configured."""
        allowed_tools = [
            {"server_name": "server1", "tool_name": "tool1"},
            {"server_name": "server2", "tool_name": "tool2"},
            {"server_name": "server1", "tool_name": "tool3"},
        ]

        # Should find tool1 on server1
        assert (
            MCPConfigService.validate_tool_access("server1", "tool1", allowed_tools)
            is True
        )

        # Should find tool2 on server2
        assert (
            MCPConfigService.validate_tool_access("server2", "tool2", allowed_tools)
            is True
        )

        # Should not find tool2 on server1
        assert (
            MCPConfigService.validate_tool_access("server1", "tool2", allowed_tools)
            is False
        )
