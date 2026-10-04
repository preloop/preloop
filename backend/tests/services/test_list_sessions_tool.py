"""The list_sessions MCP wrapper: catalog sync and the caller seam (#1045)."""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.api.endpoints.tools import BUILTIN_TOOLS
from preloop.services.initialize_mcp import initialize_mcp_with_tools

pytestmark = pytest.mark.asyncio

TOOL_NAME = "list_sessions"


@pytest.fixture
def mcp_server():
    return initialize_mcp_with_tools()


async def test_the_wrapper_signature_matches_the_catalog_schema(mcp_server):
    entries = [e for e in BUILTIN_TOOLS if e["name"] == TOOL_NAME]
    assert len(entries) == 1
    entry = entries[0]
    tool = await mcp_server.get_tool(TOOL_NAME)
    assert tool.description == entry["description"]
    assert tool.parameters == entry["schema"]
    params = {k for k in inspect.signature(tool.fn).parameters if k != "ctx"}
    assert params == set(entry["schema"]["properties"])
    assert entry["default_enabled"] is False


async def test_the_wrapper_passes_arguments_and_the_header_resolved_caller(mcp_server):
    tool = await mcp_server.get_tool(TOOL_NAME)
    user_context = MagicMock(
        account_id="acct-1",
        managed_agent_id="agent-1",
        api_key_id="key-1",
        runtime_session_id=None,
        runtime_principal_type="claude_code",
        runtime_principal_id="machine-1",
    )
    db = MagicMock()
    captured = {}

    def fake_list(db_arg, **kwargs):
        captured.update(kwargs)
        return {"scope": "own", "results": [], "total": 0, "truncated": False}

    def fake_caller_ids(db_arg, **kwargs):
        captured["caller_lookup"] = kwargs
        return ["11111111-1111-1111-1111-111111111111"]

    with (
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=user_context,
        ),
        patch(
            "preloop.services.initialize_mcp.require_approval",
            new=AsyncMock(return_value=(True, None)),
        ),
        patch(
            "preloop.models.db.session.get_db_session",
            side_effect=lambda: iter([db]),
        ),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            return_value={"x-claude-code-session-id": "ext-parent"},
        ),
        patch(
            "preloop.services.agent_session_lineage.caller_session_ids",
            side_effect=fake_caller_ids,
        ),
        patch(
            "preloop.services.agent_session_list.list_for_agent",
            side_effect=fake_list,
        ),
    ):
        out = await tool.fn(cwd="/work", agent_kind="claude_code", limit=3)

    assert json.loads(out)["scope"] == "own"
    assert captured["caller_session_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert captured["caller_lookup"]["principal_type"] == "claude_code"
    assert captured["caller_lookup"]["principal_id"] == "machine-1"
    assert captured["caller_lookup"]["headers"] == {
        "x-claude-code-session-id": "ext-parent"
    }
    assert captured["cwd"] == "/work"
    assert captured["agent_kind"] == "claude_code"
    assert captured["limit"] == 3
    assert captured["managed_agent_id"] == "agent-1"
    db.close.assert_called_once()
