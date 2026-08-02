"""Tests for DynamicFastMCP."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from mcp import types
from fastmcp import FastMCP
from fastmcp.tools import Tool

from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    create_dynamic_mcp_server,
    create_user_context_from_scope,
)
from preloop.services.dynamic_mcp_server import UserContext

pytestmark = pytest.mark.asyncio


@pytest.fixture
def user_context():
    """Create a test user context."""
    return UserContext(
        user_id=str(uuid4()),
        account_id=str(uuid4()),
        username="testuser",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )


@pytest.fixture
def dynamic_mcp():
    """Create a DynamicFastMCP instance."""
    return DynamicFastMCP("test-mcp")


class TestDynamicFastMCPInit:
    """Test DynamicFastMCP initialization."""

    def test_init_creates_instance(self):
        """Test that __init__ creates instance with proper attributes."""
        mcp = DynamicFastMCP("test-mcp")

        assert mcp._user_context_provider is None
        assert mcp._proxied_tool_servers == {}
        assert mcp._registered_proxied_tools == set()

    def test_set_user_context_provider(self, dynamic_mcp):
        """Test setting user context provider."""

        def provider():
            return UserContext(
                user_id="1",
                account_id="1",
                username="test",
                has_tracker=True,
                enabled_default_tools=[],
                enabled_proxied_tools=[],
            )

        dynamic_mcp.set_user_context_provider(provider)

        assert dynamic_mcp._user_context_provider is provider


class TestGetCurrentUserContext:
    """Test _get_current_user_context method."""

    def test_get_context_no_provider(self, dynamic_mcp):
        """Test getting context when no provider is set."""
        result = dynamic_mcp._get_current_user_context()

        assert result is None

    def test_get_context_provider_returns_context(self, dynamic_mcp, user_context):
        """Test getting context when provider returns context."""

        def provider():
            return user_context

        dynamic_mcp._user_context_provider = provider

        result = dynamic_mcp._get_current_user_context()

        assert result == user_context

    def test_get_context_provider_returns_none(self, dynamic_mcp):
        """Test getting context when provider returns None."""

        def provider():
            return None

        dynamic_mcp._user_context_provider = provider

        result = dynamic_mcp._get_current_user_context()

        assert result is None

    def test_get_context_provider_raises_error(self, dynamic_mcp):
        """Test getting context when provider raises error."""

        def error_provider():
            raise Exception("Provider error")

        dynamic_mcp._user_context_provider = error_provider

        result = dynamic_mcp._get_current_user_context()

        assert result is None


class TestListTools:
    """Test _list_tools method."""

    async def test_list_tools_no_user_context(self, dynamic_mcp):
        """Test listing tools with no user context."""
        result = await dynamic_mcp.list_tools()

        assert result == []

    async def test_list_tools_user_without_tracker(self, dynamic_mcp):
        """Test listing tools for user without tracker."""
        user_context = UserContext(
            user_id="1",
            account_id="1",
            username="test",
            has_tracker=False,
            enabled_default_tools=[],
            enabled_proxied_tools=[],
            tracker_types=[],
        )
        dynamic_mcp._user_context_provider = lambda: user_context

        # Mock database for proxied tools
        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                return_value=[],
            ):
                with patch.object(
                    FastMCP,
                    "list_tools",
                    new=AsyncMock(return_value=[]),
                ):
                    result = await dynamic_mcp.list_tools()

        # User without tracker still may get builtin tools that don't require a tracker
        assert isinstance(result, list)

    async def test_list_tools_user_with_tracker(self, dynamic_mcp, user_context):
        """Test listing tools for user with tracker."""
        dynamic_mcp._user_context_provider = lambda: user_context

        # Ensure tracker types exist
        user_context.tracker_types = ["github"]

        # Mock super().list_tools() to return default tools
        default_tools = [
            Tool(name="get_issue", description="Get issue", parameters={}),
            Tool(name="get_pull_request", description="Get PR", parameters={}),
        ]

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                return_value=[],
            ):
                with patch.object(
                    FastMCP,
                    "list_tools",
                    new=AsyncMock(return_value=default_tools),
                ):
                    result = await dynamic_mcp.list_tools()

        # Should include tools compatible with tracker types (github)
        assert any(t.name == "get_issue" for t in result)
        assert any(t.name == "get_pull_request" for t in result)

    async def test_list_tools_includes_request_approval_without_tracker(
        self, dynamic_mcp
    ):
        """Tools that do not require a tracker (e.g. request_approval) should still be visible."""
        user_context = UserContext(
            user_id="1",
            account_id="1",
            username="test",
            has_tracker=False,
            enabled_default_tools=[],
            enabled_proxied_tools=[],
            tracker_types=[],
        )
        dynamic_mcp._user_context_provider = lambda: user_context

        default_tools = [
            Tool(
                name="request_approval", description="Request approval", parameters={}
            ),
            Tool(name="get_issue", description="Get issue", parameters={}),
        ]

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                return_value=[],
            ):
                with patch.object(
                    FastMCP,
                    "list_tools",
                    new=AsyncMock(return_value=default_tools),
                ):
                    result = await dynamic_mcp.list_tools()

        assert any(t.name == "request_approval" for t in result)
        assert not any(t.name == "get_issue" for t in result)

    async def test_list_tools_filters_internal_names(self, dynamic_mcp, user_context):
        """Test that internal tool names (account_*) are filtered out."""
        dynamic_mcp._user_context_provider = lambda: user_context

        # Mock tools including internal names
        default_tools = [
            Tool(name="public_tool", description="Public", parameters={}),
            Tool(
                name="account_123_internal",
                description="Internal",
                parameters={},
            ),
        ]

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                return_value=[],
            ):
                with patch.object(
                    FastMCP,
                    "list_tools",
                    new=AsyncMock(return_value=default_tools),
                ):
                    result = await dynamic_mcp.list_tools()

        # Only public tool should be included
        assert len(result) == 1
        assert result[0].name == "public_tool"

    async def test_list_tools_flow_execution_allows_zero_tools(self, dynamic_mcp):
        """Empty allowed_flow_tools list should restrict to zero tools.

        This is a security behavior: an explicit empty allow-list should NOT be
        treated as "no restriction".
        """

        user_context = UserContext(
            user_id="1",
            account_id="1",
            username="test",
            has_tracker=True,
            enabled_default_tools=[],
            enabled_proxied_tools=[],
            tracker_types=["github"],
            flow_execution_id="flow-exec-1",
            allowed_flow_tools=[],
        )
        dynamic_mcp._user_context_provider = lambda: user_context

        default_tools = [
            Tool(
                name="request_approval", description="Request approval", parameters={}
            ),
            Tool(name="get_issue", description="Get issue", parameters={}),
        ]

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                return_value=[],
            ):
                with patch.object(
                    FastMCP,
                    "list_tools",
                    new=AsyncMock(return_value=default_tools),
                ):
                    result = await dynamic_mcp.list_tools()

        assert result == []

    async def test_list_tools_includes_proxied_tools(self, dynamic_mcp, user_context):
        """Test that proxied tools are included in tool list."""
        dynamic_mcp._user_context_provider = lambda: user_context

        # Mock proxied tool data
        mock_mcp_server = MagicMock()
        mock_mcp_server.id = str(uuid4())

        mock_mcp_tool = MagicMock()
        mock_mcp_tool.name = "proxied_tool"
        mock_mcp_tool.description = "Proxied Tool"
        mock_mcp_tool.input_schema = {"properties": {}}

        # Create an internal tool that will be "registered"
        safe_account_id = user_context.account_id.replace("-", "_")
        internal_name = f"account_{safe_account_id}_proxied_tool"
        registered_tool = Tool(
            name=internal_name, description="Internal", parameters={}
        )

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                return_value=[(mock_mcp_server, mock_mcp_tool)],
            ):
                # Mock super().list_tools to return the "registered" internal tool
                with patch.object(
                    FastMCP,
                    "list_tools",
                    new=AsyncMock(return_value=[registered_tool]),
                ):
                    # Mock tool registration
                    with patch.object(dynamic_mcp, "tool", return_value=lambda x: x):
                        result = await dynamic_mcp.list_tools()

        # Should include proxied tool with original name (not internal name)
        assert any(t.name == "proxied_tool" for t in result)
        # Should NOT include internal name in results
        assert not any(t.name == internal_name for t in result)

    async def test_list_tools_excludes_explicitly_disabled_builtin(
        self, dynamic_mcp, user_context
    ):
        """A builtin tool with an is_enabled=False config row must be hidden."""
        dynamic_mcp._user_context_provider = lambda: user_context
        user_context.tracker_types = ["github"]

        default_tools = [
            Tool(name="get_issue", description="Get issue", parameters={}),
            Tool(name="create_issue", description="Create issue", parameters={}),
        ]

        disabled_config = MagicMock()
        disabled_config.tool_name = "get_issue"
        disabled_config.tool_source = "builtin"
        disabled_config.is_enabled = False
        disabled_config.justification_mode = None
        disabled_config.managed_agent_id = None

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with (
                patch(
                    "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                    return_value=[],
                ),
                patch(
                    "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                    return_value=[disabled_config],
                ),
                patch(
                    "preloop.models.crud.crud_account.get",
                    return_value=MagicMock(meta_data={}),
                ),
                patch.object(
                    FastMCP, "list_tools", new=AsyncMock(return_value=default_tools)
                ),
            ):
                result = await dynamic_mcp.list_tools()

        names = {t.name for t in result}
        assert "get_issue" not in names
        assert "create_issue" in names

    async def test_list_tools_excludes_default_disabled_builtin_without_config(
        self, dynamic_mcp, user_context
    ):
        """Default-disabled compliance tools are hidden when no config exists."""
        dynamic_mcp._user_context_provider = lambda: user_context
        user_context.tracker_types = ["github"]

        default_tools = [
            Tool(name="estimate_compliance", description="EC", parameters={}),
            Tool(name="improve_compliance", description="IC", parameters={}),
            Tool(name="get_issue", description="Get issue", parameters={}),
        ]

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with (
                patch(
                    "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                    return_value=[],
                ),
                patch(
                    "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                    return_value=[],
                ),
                patch(
                    "preloop.models.crud.crud_account.get",
                    return_value=MagicMock(meta_data={}),
                ),
                patch.object(
                    FastMCP, "list_tools", new=AsyncMock(return_value=default_tools)
                ),
            ):
                result = await dynamic_mcp.list_tools()

        names = {t.name for t in result}
        assert "estimate_compliance" not in names
        assert "improve_compliance" not in names
        assert "get_issue" in names

    async def test_list_tools_explicit_enable_overrides_default_disabled(
        self, dynamic_mcp, user_context
    ):
        """An explicit is_enabled=True config re-enables a default-disabled tool."""
        dynamic_mcp._user_context_provider = lambda: user_context
        user_context.tracker_types = ["github"]

        default_tools = [
            Tool(name="estimate_compliance", description="EC", parameters={}),
            Tool(name="improve_compliance", description="IC", parameters={}),
        ]

        enable_config = MagicMock()
        enable_config.tool_name = "estimate_compliance"
        enable_config.tool_source = "builtin"
        enable_config.is_enabled = True
        enable_config.justification_mode = None
        enable_config.managed_agent_id = None

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with (
                patch(
                    "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                    return_value=[],
                ),
                patch(
                    "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                    return_value=[enable_config],
                ),
                patch(
                    "preloop.models.crud.crud_account.get",
                    return_value=MagicMock(meta_data={}),
                ),
                patch.object(
                    FastMCP, "list_tools", new=AsyncMock(return_value=default_tools)
                ),
            ):
                result = await dynamic_mcp.list_tools()

        names = {t.name for t in result}
        assert "estimate_compliance" in names
        assert "improve_compliance" not in names

    async def _list_tools_with_configs(self, dynamic_mcp, user_context, configs):
        """Run list_tools with the given ToolConfiguration rows mocked in."""
        default_tools = [
            Tool(name="permission_prompt", description="PP", parameters={}),
            Tool(name="get_issue", description="Get issue", parameters={}),
        ]

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with (
                patch(
                    "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                    return_value=[],
                ),
                patch(
                    "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                    return_value=configs,
                ),
                patch(
                    "preloop.models.crud.crud_account.get",
                    return_value=MagicMock(meta_data={}),
                ),
                patch.object(
                    FastMCP, "list_tools", new=AsyncMock(return_value=default_tools)
                ),
            ):
                return await dynamic_mcp.list_tools()

    @staticmethod
    def _agent_scoped_enable(tool_name, agent_id, is_enabled=True):
        config = MagicMock()
        config.tool_name = tool_name
        config.tool_source = "builtin"
        config.is_enabled = is_enabled
        config.justification_mode = None
        config.managed_agent_id = agent_id
        return config

    async def test_list_tools_agent_scoped_enable_visible_to_that_agent(
        self, dynamic_mcp, user_context
    ):
        """An agent-scoped enable exposes a default-disabled tool to that agent."""
        user_context.tracker_types = ["github"]
        user_context.managed_agent_id = "agent-1"
        dynamic_mcp._user_context_provider = lambda: user_context

        result = await self._list_tools_with_configs(
            dynamic_mcp,
            user_context,
            [self._agent_scoped_enable("permission_prompt", "agent-1")],
        )

        names = {t.name for t in result}
        assert "permission_prompt" in names

    async def test_list_tools_agent_scoped_enable_hidden_from_other_agents(
        self, dynamic_mcp, user_context
    ):
        """Other agents of the account must not see (or pay context for) the
        tool enabled for one agent."""
        user_context.tracker_types = ["github"]
        user_context.managed_agent_id = "agent-2"
        dynamic_mcp._user_context_provider = lambda: user_context

        result = await self._list_tools_with_configs(
            dynamic_mcp,
            user_context,
            [self._agent_scoped_enable("permission_prompt", "agent-1")],
        )

        names = {t.name for t in result}
        assert "permission_prompt" not in names

    async def test_list_tools_agent_scoped_enable_hidden_without_agent_identity(
        self, dynamic_mcp, user_context
    ):
        """Callers with no managed-agent identity fall back to the account
        default: default-disabled tools stay hidden."""
        user_context.tracker_types = ["github"]
        user_context.managed_agent_id = None
        dynamic_mcp._user_context_provider = lambda: user_context

        result = await self._list_tools_with_configs(
            dynamic_mcp,
            user_context,
            [self._agent_scoped_enable("permission_prompt", "agent-1")],
        )

        names = {t.name for t in result}
        assert "permission_prompt" not in names

    async def test_list_tools_agent_scoped_disable_overrides_account_enable(
        self, dynamic_mcp, user_context
    ):
        """An agent-scoped row wins over the account-wide row for that agent."""
        user_context.tracker_types = ["github"]
        user_context.managed_agent_id = "agent-1"
        dynamic_mcp._user_context_provider = lambda: user_context

        account_enable = self._agent_scoped_enable("get_issue", None, is_enabled=True)
        agent_disable = self._agent_scoped_enable(
            "get_issue", "agent-1", is_enabled=False
        )

        result = await self._list_tools_with_configs(
            dynamic_mcp, user_context, [account_enable, agent_disable]
        )

        names = {t.name for t in result}
        assert "get_issue" not in names

    async def test_list_tools_flow_execution_bypasses_enable_filter(self, dynamic_mcp):
        """Flow executions with an explicit allow-list see their tools even if
        the account disabled them (presets opt in explicitly)."""
        user_context = UserContext(
            user_id="1",
            account_id="1",
            username="test",
            has_tracker=True,
            enabled_default_tools=[],
            enabled_proxied_tools=[],
            tracker_types=["github"],
            flow_execution_id="flow-exec-1",
            allowed_flow_tools=["get_issue", "estimate_compliance"],
        )
        dynamic_mcp._user_context_provider = lambda: user_context

        default_tools = [
            Tool(name="get_issue", description="Get issue", parameters={}),
            Tool(name="estimate_compliance", description="EC", parameters={}),
            Tool(name="create_issue", description="Create issue", parameters={}),
        ]

        disabled_config = MagicMock()
        disabled_config.tool_name = "get_issue"
        disabled_config.tool_source = "builtin"
        disabled_config.is_enabled = False
        disabled_config.justification_mode = None
        disabled_config.managed_agent_id = None

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with (
                patch(
                    "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                    return_value=[],
                ),
                patch(
                    "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                    return_value=[disabled_config],
                ),
                patch(
                    "preloop.models.crud.crud_account.get",
                    return_value=MagicMock(meta_data={}),
                ),
                patch.object(
                    FastMCP, "list_tools", new=AsyncMock(return_value=default_tools)
                ),
            ):
                result = await dynamic_mcp.list_tools()

        names = {t.name for t in result}
        # Account-disabled get_issue and default-disabled estimate_compliance
        # are still visible because the flow explicitly allows them.
        assert names == {"get_issue", "estimate_compliance"}

    async def test_list_tools_error_loading_proxied(self, dynamic_mcp, user_context):
        """Test handling error when loading proxied tools."""
        dynamic_mcp._user_context_provider = lambda: user_context

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
                side_effect=Exception("DB Error"),
            ):
                with patch.object(
                    FastMCP,
                    "list_tools",
                    new=AsyncMock(return_value=[]),
                ):
                    # Should not raise, just continue with default tools
                    result = await dynamic_mcp.list_tools()

        assert isinstance(result, list)


class TestMCPCallTool:
    """Test call_tool method (FastMCP 3.x+)."""

    async def test_call_tool_no_user_context(self, dynamic_mcp):
        """Test calling tool with no user context."""
        from fastmcp.tools.tool import ToolResult

        result = await dynamic_mcp.call_tool("tool1", {})

        assert isinstance(result, ToolResult)
        assert len(result.content) == 1
        assert "No user context available" in result.content[0].text

    async def test_call_tool_unauthorized(self, dynamic_mcp, user_context):
        """Test calling unauthorized tool."""
        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context

        # Mock list_tools to return empty list
        with patch.object(dynamic_mcp, "list_tools", return_value=[]):
            result = await dynamic_mcp.call_tool("unauthorized_tool", {})

        assert isinstance(result, ToolResult)
        assert len(result.content) == 1
        assert "Access denied" in result.content[0].text

    async def test_call_builtin_tool(self, dynamic_mcp, user_context):
        """Test calling builtin tool."""
        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context

        # Mock list_tools to include the tool
        available_tools = [
            Tool(name="builtin_tool", description="Builtin", parameters={})
        ]

        with (
            patch.object(dynamic_mcp, "list_tools", return_value=available_tools),
            patch(
                "preloop.services.policy_evaluator.evaluate_policy_async",
                new=AsyncMock(return_value=("allow", None, None)),
            ),
        ):
            # Mock super().call_tool for FastMCP 3.x
            mock_result = ToolResult(
                content=[types.TextContent(type="text", text="Result")]
            )
            with patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(return_value=mock_result),
                create=True,
            ):
                result = await dynamic_mcp.call_tool("builtin_tool", {})

        assert isinstance(result, ToolResult)
        assert result.content[0].text == "Result"

    async def test_call_proxied_tool_translates_name(self, dynamic_mcp, user_context):
        """Test calling proxied tool translates name."""
        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context
        dynamic_mcp._proxied_tool_servers["proxied_tool"] = "server-id"

        # Mock list_tools to include the tool
        available_tools = [
            Tool(name="proxied_tool", description="Proxied", parameters={})
        ]

        with (
            patch.object(dynamic_mcp, "list_tools", return_value=available_tools),
            patch(
                "preloop.services.policy_evaluator.evaluate_policy_async",
                new=AsyncMock(return_value=("allow", None, None)),
            ),
        ):
            # Mock super().call_tool to verify name translation
            mock_result = ToolResult(
                content=[types.TextContent(type="text", text="Result")]
            )
            with patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(return_value=mock_result),
                create=True,
            ) as mock_super:
                await dynamic_mcp.call_tool("proxied_tool", {})

                # Verify internal name was used dynamically
                safe_account_id = user_context.account_id.replace("-", "_")
                expected_internal_name = f"account_{safe_account_id}_proxied_tool"
                mock_super.assert_called_once_with(
                    expected_internal_name,
                    {},
                    version=None,
                    run_middleware=True,
                    task_meta=None,
                )

    async def test_call_disabled_builtin_tool_rejected(self, dynamic_mcp, user_context):
        """A builtin tool disabled by ToolConfiguration cannot be invoked by name."""
        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context

        disabled_config = MagicMock()
        disabled_config.tool_name = "get_issue"
        disabled_config.tool_source = "builtin"
        disabled_config.is_enabled = False
        disabled_config.justification_mode = None
        disabled_config.managed_agent_id = None

        with (
            patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db,
            patch(
                "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                return_value=[disabled_config],
            ),
            patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(),
                create=True,
            ) as mock_super,
        ):
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            result = await dynamic_mcp.call_tool("get_issue", {"issue": "ABC-1"})

        mock_super.assert_not_called()
        assert isinstance(result, ToolResult)
        assert "disabled" in result.content[0].text.lower()

    async def test_call_default_disabled_builtin_tool_rejected(
        self, dynamic_mcp, user_context
    ):
        """A default-disabled builtin tool with no config cannot be invoked."""
        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context

        with (
            patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db,
            patch(
                "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                return_value=[],
            ),
            patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(),
                create=True,
            ) as mock_super,
        ):
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            result = await dynamic_mcp.call_tool(
                "estimate_compliance", {"issues": ["ABC-1"]}
            )

        mock_super.assert_not_called()
        assert isinstance(result, ToolResult)
        assert "disabled" in result.content[0].text.lower()

    async def test_call_disabled_permission_prompt_returns_behavior_schema(
        self, dynamic_mcp, user_context
    ):
        """A disabled permission_prompt must deny in Claude's behavior schema.

        Claude Code parses this tool's text response as the permission
        behavior schema; a plain access-denied string would surface as a
        parse failure instead of a clean deny.
        """
        import json as _json

        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context

        with (
            patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db,
            patch(
                "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                return_value=[],
            ),
            patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(),
                create=True,
            ) as mock_super,
        ):
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            result = await dynamic_mcp.call_tool(
                "permission_prompt",
                {"tool_name": "Bash", "input": {"command": "ls"}},
            )

        mock_super.assert_not_called()
        assert isinstance(result, ToolResult)
        behavior = _json.loads(result.content[0].text)
        assert behavior["behavior"] == "deny"
        assert "Tools page" in behavior["message"]

    async def test_call_permission_prompt_agent_scoped_enable_allows_caller(
        self, dynamic_mcp, user_context
    ):
        """An agent-scoped enable permits the call for exactly that agent."""
        from fastmcp.tools.tool import ToolResult

        user_context.managed_agent_id = "agent-1"
        dynamic_mcp._user_context_provider = lambda: user_context

        agent_config = MagicMock()
        agent_config.tool_name = "permission_prompt"
        agent_config.tool_source = "builtin"
        agent_config.is_enabled = True
        agent_config.justification_mode = None
        agent_config.managed_agent_id = "agent-1"

        available_tools = [
            Tool(name="permission_prompt", description="PP", parameters={})
        ]

        mock_result = ToolResult(
            content=[types.TextContent(type="text", text="Result")]
        )
        with (
            patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db,
            patch(
                "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                return_value=[agent_config],
            ),
            patch.object(dynamic_mcp, "list_tools", return_value=available_tools),
            patch(
                "preloop.services.policy_evaluator.evaluate_policy_async",
                new=AsyncMock(return_value=("allow", None, None)),
            ),
            patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(return_value=mock_result),
                create=True,
            ) as mock_super,
        ):
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            result = await dynamic_mcp.call_tool(
                "permission_prompt",
                {"tool_name": "Bash", "input": {"command": "ls"}},
            )

        mock_super.assert_called_once()
        assert isinstance(result, ToolResult)
        assert result.content[0].text == "Result"

    async def test_call_permission_prompt_scoped_to_other_agent_denied(
        self, dynamic_mcp, user_context
    ):
        """A row scoped to another agent must not permit this caller: the
        default-disabled state applies and the deny keeps Claude's behavior
        schema."""
        import json as _json

        from fastmcp.tools.tool import ToolResult

        user_context.managed_agent_id = "agent-2"
        dynamic_mcp._user_context_provider = lambda: user_context

        agent_config = MagicMock()
        agent_config.tool_name = "permission_prompt"
        agent_config.tool_source = "builtin"
        agent_config.is_enabled = True
        agent_config.justification_mode = None
        agent_config.managed_agent_id = "agent-1"

        with (
            patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db,
            patch(
                "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                return_value=[agent_config],
            ),
            patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(),
                create=True,
            ) as mock_super,
        ):
            mock_db = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            result = await dynamic_mcp.call_tool(
                "permission_prompt",
                {"tool_name": "Bash", "input": {"command": "ls"}},
            )

        mock_super.assert_not_called()
        assert isinstance(result, ToolResult)
        behavior = _json.loads(result.content[0].text)
        assert behavior["behavior"] == "deny"
        assert "Tools page" in behavior["message"]

    async def test_call_tool_require_approval_without_workflow_blocks(
        self, dynamic_mcp, user_context
    ):
        """If the policy returns ``require_approval`` but no workflow can be
        resolved (no rule, config, or account default), the call must be
        blocked rather than silently allowed through.

        Without this fail-closed behaviour an explicit ``require_approval``
        rule whose workflow is unset would behave like ``allow``, which is
        the exact silent-bypass bug we're guarding against.
        """
        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context

        available_tools = [Tool(name="pay", description="Pay tool", parameters={})]

        # ``evaluate_policy_async`` returns require_approval but no workflow id
        with patch.object(dynamic_mcp, "list_tools", return_value=available_tools):
            with patch(
                "preloop.services.policy_evaluator.evaluate_policy_async",
                new=AsyncMock(
                    return_value=("require_approval", None, "matched bare rule")
                ),
            ):
                with patch(
                    "preloop.models.db.session.get_async_db_session"
                ) as mock_session:
                    mock_session.return_value.__aenter__ = AsyncMock(
                        return_value=MagicMock()
                    )
                    mock_session.return_value.__aexit__ = AsyncMock(return_value=None)

                    with patch.object(
                        dynamic_mcp.__class__.__bases__[0],
                        "call_tool",
                        new=AsyncMock(),
                        create=True,
                    ) as mock_super:
                        result = await dynamic_mcp.call_tool(
                            "pay", {"amount": 10, "recipient": "alice"}
                        )

        # Tool must NOT have been executed.
        mock_super.assert_not_called()
        assert isinstance(result, ToolResult)
        assert "approval workflow" in result.content[0].text.lower()
        assert "configure" in result.content[0].text.lower()

    async def test_call_tool_internal_name_reentry_skips_access_check(
        self, dynamic_mcp, user_context
    ):
        """Re-entering call_tool with a registered internal name must
        bypass the access check.

        FastMCP's tool dispatcher re-routes super().call_tool(internal_name)
        through this subclass. Because list_tools strips ``account_*`` names
        from the user-visible tool list, the re-entry path would otherwise
        fail with ``Access denied`` and break every proxied tool that has an
        access rule (e.g. ``require_approval``).
        """
        from fastmcp.tools.tool import ToolResult

        dynamic_mcp._user_context_provider = lambda: user_context
        safe_account_id = user_context.account_id.replace("-", "_")
        internal_name = f"account_{safe_account_id}_pay"
        dynamic_mcp._registered_proxied_tools.add(internal_name)

        list_tools_mock = AsyncMock()
        mock_result = ToolResult(content=[types.TextContent(type="text", text="Paid")])

        from preloop.services.dynamic_fastmcp import _is_proxy_translation_var

        with patch.object(dynamic_mcp, "list_tools", new=list_tools_mock):
            with patch.object(
                dynamic_mcp.__class__.__bases__[0],
                "call_tool",
                new=AsyncMock(return_value=mock_result),
                create=True,
            ) as mock_super:
                token = _is_proxy_translation_var.set(True)
                try:
                    result = await dynamic_mcp.call_tool(
                        internal_name, {"amount": 10, "recipient": "alice"}
                    )
                finally:
                    _is_proxy_translation_var.reset(token)

        list_tools_mock.assert_not_called()
        mock_super.assert_called_once_with(
            internal_name,
            {"amount": 10, "recipient": "alice"},
            version=None,
            run_middleware=True,
            task_meta=None,
        )
        assert isinstance(result, ToolResult)
        assert result.content[0].text == "Paid"


class TestCreateProxiedToolWrapper:
    """Test _create_proxied_tool_wrapper method."""

    def test_create_wrapper_simple_params(self, dynamic_mcp, user_context):
        """Test creating wrapper with simple parameters."""
        wrapper = dynamic_mcp._create_proxied_tool_wrapper(
            tool_name="test_tool",
            server_id="server-123",
            account_id=user_context.account_id,
            description="Test tool",
            input_schema={
                "properties": {"param1": {"type": "string"}},
                "required": ["param1"],
            },
        )

        assert callable(wrapper)
        assert wrapper.__doc__ == "Test tool"
        assert wrapper._display_name == "test_tool"
        assert wrapper._account_id == user_context.account_id

    def test_create_wrapper_optional_params(self, dynamic_mcp, user_context):
        """Test creating wrapper with optional parameters."""
        wrapper = dynamic_mcp._create_proxied_tool_wrapper(
            tool_name="test_tool",
            server_id="server-123",
            account_id=user_context.account_id,
            description="Test tool",
            input_schema={
                "properties": {
                    "required_param": {"type": "string"},
                    "optional_param": {"type": "integer"},
                },
                "required": ["required_param"],
            },
        )

        assert callable(wrapper)

    def test_create_wrapper_various_types(self, dynamic_mcp, user_context):
        """Test creating wrapper with various parameter types."""
        wrapper = dynamic_mcp._create_proxied_tool_wrapper(
            tool_name="test_tool",
            server_id="server-123",
            account_id=user_context.account_id,
            description="Test tool",
            input_schema={
                "properties": {
                    "str_param": {"type": "string"},
                    "int_param": {"type": "integer"},
                    "float_param": {"type": "number"},
                    "bool_param": {"type": "boolean"},
                    "list_param": {"type": "array"},
                    "dict_param": {"type": "object"},
                },
                "required": [],
            },
        )

        assert callable(wrapper)


class TestHelperFunctions:
    """Test helper functions."""

    def test_create_dynamic_mcp_server(self):
        """Test create_dynamic_mcp_server creates instance."""
        mcp = create_dynamic_mcp_server()

        assert isinstance(mcp, DynamicFastMCP)

    def test_create_user_context_no_authenticated_user(self):
        """Test creating user context with no authenticated user."""
        scope = {"user": None}

        result = create_user_context_from_scope(scope)

        assert result is None

    def test_create_user_context_no_account(self):
        """Test creating user context when user has no account."""
        # Create mock authenticated user with no account
        mock_user = MagicMock()
        mock_user.access_token = MagicMock()
        mock_user.access_token.account = None

        scope = {"user": mock_user}

        result = create_user_context_from_scope(scope)

        assert result is None

    def test_create_user_context_success(self):
        """Test successfully creating user context."""
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        # Create mock account
        mock_account = MagicMock()
        mock_account.id = str(uuid4())

        # Create mock user object (what would be in access_token.user)
        mock_db_user = MagicMock()
        mock_db_user.id = str(uuid4())
        mock_db_user.username = "testuser"
        mock_db_user.account_id = mock_account.id
        mock_db_user.account = mock_account

        # Use spec to make isinstance() work
        mock_user = MagicMock(spec=AuthenticatedUser)
        mock_user.access_token = MagicMock()
        mock_user.access_token.user = mock_db_user

        scope = {"user": mock_user}

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.dynamic_fastmcp.has_tracker",
                return_value=True,
            ):
                result = create_user_context_from_scope(scope)

        assert result is not None
        assert result.username == "testuser"
        assert result.has_tracker is True

    def test_create_user_context_flow_execution_allows_zero_tools(self):
        """Empty allowed_mcp_tools should translate to an explicit empty allow-list."""
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        # Create mock account
        mock_account = MagicMock()
        mock_account.id = str(uuid4())

        # Create mock user object (what would be in access_token.user)
        mock_db_user = MagicMock()
        mock_db_user.id = str(uuid4())
        mock_db_user.username = "testuser"
        mock_db_user.account_id = mock_account.id
        mock_db_user.account = mock_account

        mock_api_key = MagicMock()
        mock_api_key.context_data = {
            "flow_execution_id": "flow-exec-1",
            "allowed_mcp_tools": [],
            "runtime_principal": {
                "type": "flow_execution",
                "id": "flow-exec-1",
                "name": "Test Flow",
            },
        }

        # Use spec to make isinstance() work
        mock_user = MagicMock(spec=AuthenticatedUser)
        mock_user.access_token = MagicMock()
        mock_user.access_token.user = mock_db_user
        mock_user.access_token.api_key = mock_api_key

        scope = {"user": mock_user}

        with patch("preloop.services.dynamic_fastmcp.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.close = MagicMock()
            mock_get_db.side_effect = lambda: iter([mock_db])

            with patch(
                "preloop.services.dynamic_fastmcp.has_tracker",
                return_value=True,
            ):
                result = create_user_context_from_scope(scope)

        assert result is not None
        assert result.flow_execution_id == "flow-exec-1"
        assert result.allowed_flow_tools == []
        assert result.runtime_principal_type == "flow_execution"
        assert result.runtime_principal_id == "flow-exec-1"
        assert result.runtime_principal_name == "Test Flow"
