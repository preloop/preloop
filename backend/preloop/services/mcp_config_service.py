"""Service for generating MCP configuration for agent containers."""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPConfigService:
    """
    Service for generating MCP configuration for agent execution.

    Generates configuration files and environment variables that agents
    can use to interact with allowed MCP servers and tools.
    """

    @staticmethod
    def generate_mcp_config(
        allowed_mcp_servers: List[str],
        allowed_mcp_tools: List[Dict[str, str]],
        preloop_url: Optional[str] = None,
        account_api_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate MCP configuration for an agent.

        Args:
            allowed_mcp_servers: List of allowed MCP server names
            allowed_mcp_tools: List of allowed MCP tool definitions
            preloop_url: Base URL for Preloop MCP endpoints
            account_api_token: API token for the account (for Preloop MCP access)

        Returns:
            MCP configuration dict that can be mounted as JSON file
        """
        if preloop_url is None:
            preloop_url = os.getenv("PRELOOP_URL", "http://host.docker.internal:8000")

        config = {
            "mcpServers": {},
            "allowed_tools": {},
        }

        # Build MCP server configurations
        for server_name in allowed_mcp_servers:
            if server_name == "preloop-mcp":
                # Preloop MCP endpoints with authentication
                server_config = {
                    "url": f"{preloop_url}/mcp/v1",
                    "transport": "http-streaming",
                }

                # Add authentication if token is provided
                if account_api_token:
                    server_config["headers"] = {
                        "Authorization": f"Bearer {account_api_token}"
                    }

                config["mcpServers"]["preloop-mcp"] = server_config
            else:
                # Other MCP servers can be configured here
                logger.warning(f"Unknown MCP server: {server_name}, skipping")

        # Build allowed tools map for filtering
        for tool in allowed_mcp_tools:
            server_name = tool.get("server_name")
            tool_name = tool.get("tool_name")

            if server_name and tool_name:
                if server_name not in config["allowed_tools"]:
                    config["allowed_tools"][server_name] = []
                config["allowed_tools"][server_name].append(tool_name)

        logger.debug(f"Generated MCP config: {json.dumps(config, indent=2)}")
        return config

    @staticmethod
    def generate_mcp_environment_vars(
        allowed_mcp_servers: List[str],
        allowed_mcp_tools: List[Dict[str, str]],
    ) -> Dict[str, str]:
        """
        Generate environment variables for MCP configuration.

        Args:
            allowed_mcp_servers: List of allowed MCP server names
            allowed_mcp_tools: List of allowed MCP tool definitions

        Returns:
            Dictionary of environment variables
        """
        env = {}

        # Set allowed MCP servers as comma-separated list
        if allowed_mcp_servers:
            env["MCP_ALLOWED_SERVERS"] = ",".join(allowed_mcp_servers)

        # Set allowed tools as JSON string
        if allowed_mcp_tools:
            # Create a simplified mapping for env var
            tools_map = {}
            for tool in allowed_mcp_tools:
                server_name = tool.get("server_name")
                tool_name = tool.get("tool_name")
                if server_name and tool_name:
                    if server_name not in tools_map:
                        tools_map[server_name] = []
                    tools_map[server_name].append(tool_name)

            env["MCP_ALLOWED_TOOLS"] = json.dumps(tools_map)

        # Set Preloop MCP endpoint
        preloop_url = os.getenv("PRELOOP_URL", "http://host.docker.internal:8000")
        env["PRELOOP_MCP_URL"] = f"{preloop_url}/mcp/v1"

        return env

    @staticmethod
    def validate_tool_access(
        server_name: str,
        tool_name: str,
        allowed_mcp_tools: List[Dict[str, str]],
    ) -> bool:
        """
        Check if a tool is allowed based on the configuration.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool
            allowed_mcp_tools: List of allowed tool definitions

        Returns:
            True if tool access is allowed, False otherwise
        """
        for tool in allowed_mcp_tools:
            if (
                tool.get("server_name") == server_name
                and tool.get("tool_name") == tool_name
            ):
                return True
        return False
