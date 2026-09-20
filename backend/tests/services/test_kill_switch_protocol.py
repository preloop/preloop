"""Kill-switch refusals survive MCP success-output schema validation."""

from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_halt_refusal_reaches_mcp_client(unavailable: bool) -> None:
    """The protocol returns the halt reason without executing the tool."""
    server = DynamicFastMCP("halt-protocol")
    server._user_context_provider = lambda: UserContext(
        user_id="00000000-0000-4000-8000-000000000001",
        account_id="00000000-0000-4000-8000-000000000002",
        username="test-user",
    )
    called = False

    @server.tool()
    def guarded_tool() -> str:
        """Return a schema-bearing result only when allowed."""
        nonlocal called
        called = True
        return "success"

    with (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([MagicMock()]),
        ),
        patch(
            "preloop.services.dynamic_fastmcp.kill_switch_service.tools_halted",
            return_value=True,
            side_effect=RuntimeError("unavailable") if unavailable else None,
        ),
    ):
        async with Client(server) as client:
            result = await client.call_tool("guarded_tool", {}, raise_on_error=False)

    assert result.is_error
    text = " ".join(item.text for item in result.content)
    assert "halt state" in text if unavailable else "kill switch" in text
    assert "Output validation error" not in text
    assert not called


@pytest.mark.asyncio
async def test_access_rule_deny_reaches_mcp_client() -> None:
    """Access-rule denials must also skip output-schema validation."""
    from unittest.mock import AsyncMock

    server = DynamicFastMCP("deny-protocol")
    server._user_context_provider = lambda: UserContext(
        user_id="00000000-0000-4000-8000-000000000001",
        account_id="00000000-0000-4000-8000-000000000002",
        username="test-user",
    )
    called = False

    @server.tool()
    def guarded_tool() -> str:
        """Return a schema-bearing result only when allowed."""
        nonlocal called
        called = True
        return "success"

    with (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([MagicMock()]),
        ),
        patch(
            "preloop.services.dynamic_fastmcp.kill_switch_service.tools_halted",
            return_value=False,
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
            return_value=[],
        ),
        patch("preloop.models.db.session.get_async_db_session") as mock_session,
        patch(
            "preloop.services.policy_evaluator.evaluate_policy_async",
            new=AsyncMock(return_value=("deny", None, "blocked by test")),
        ),
    ):
        mock_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_session.return_value.__aexit__ = AsyncMock(return_value=None)
        async with Client(server) as client:
            result = await client.call_tool("guarded_tool", {}, raise_on_error=False)

    assert result.is_error
    text = " ".join(item.text for item in result.content)
    assert "Access denied" in text
    assert "blocked by test" in text
    assert "Output validation error" not in text
    assert not called
