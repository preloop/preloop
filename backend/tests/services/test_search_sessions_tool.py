"""The search_sessions tool as an agent meets it: catalog, gating, guards.

The scope rules and the size cap that decide one answer live in
agent_session_search and are tested against a database there. What is tested
here is the surface: that the REST catalogue and the callable describe one
tool, that an operator policy can switch it off per preset, and that the
identity the scope is built from comes from the session rather than from the
arguments.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool

from preloop.api.endpoints.tools import BUILTIN_TOOLS
from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from preloop.tools.builtin_defs import (
    SEARCH_SESSIONS_DEFAULT_LIMIT,
    SEARCH_SESSIONS_MAX_LIMIT,
    SEARCH_SESSIONS_TOOL,
)

pytestmark = pytest.mark.asyncio

TOOL_NAME = "search_sessions"


@pytest.fixture
def mcp_server():
    """Registrations only: no server, no provider connection."""
    return initialize_mcp_with_tools()


def _catalog_entry():
    entries = [entry for entry in BUILTIN_TOOLS if entry["name"] == TOOL_NAME]
    assert len(entries) == 1
    return entries[0]


def _flow_context(*, allowed, principal="agent-1"):
    return UserContext(
        user_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        username="flow",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        tracker_types=["github"],
        flow_execution_id="flow-exec-1",
        runtime_principal_id=principal,
        allowed_flow_tools=list(allowed),
    )


# --- the catalog and the callable describe one tool ------------------------


async def test_the_tool_is_in_the_listing_and_cannot_drift(mcp_server):
    """One definition (builtin_defs) reaches both the REST list and MCP."""
    entry = _catalog_entry()
    tool = await mcp_server.get_tool(TOOL_NAME)
    assert tool is not None
    assert tool.description == entry["description"]
    assert tool.parameters == entry["schema"]
    assert tool.parameters["additionalProperties"] is False

    parameters = {
        key: parameter
        for key, parameter in inspect.signature(tool.fn).parameters.items()
        if key != "ctx"
    }
    assert set(parameters) == set(entry["schema"]["properties"])
    required = {
        key
        for key, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    assert required == set(entry["schema"]["required"]) == {"query"}


async def test_the_schema_covers_the_query_time_range_limit_scope_and_mode():
    """The four things a search is, plus the scope that bounds it."""
    properties = SEARCH_SESSIONS_TOOL["schema"]["properties"]
    assert set(properties) == {
        "query",
        "scope",
        "mode",
        "start_date",
        "end_date",
        "limit",
    }
    assert properties["scope"]["enum"] == ["own", "account"]
    assert properties["mode"]["enum"] == ["keyword", "semantic", "hybrid"]
    assert properties["limit"]["maximum"] == SEARCH_SESSIONS_MAX_LIMIT
    assert properties["start_date"]["format"] == "date-time"
    assert properties["end_date"]["format"] == "date-time"


async def test_the_default_limit_is_the_one_the_schema_documents(mcp_server):
    """A caller that says nothing gets the page size the description names."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    default = inspect.signature(tool.fn).parameters["limit"].default
    assert default == SEARCH_SESSIONS_DEFAULT_LIMIT
    assert (
        str(SEARCH_SESSIONS_DEFAULT_LIMIT)
        in (SEARCH_SESSIONS_TOOL["schema"]["properties"]["limit"]["description"])
    )


async def test_the_description_tells_the_agent_what_a_refusal_looks_like():
    """A refusal is a code the agent can act on, not prose to parse."""
    description = _catalog_entry()["description"]
    assert "account_scope_not_granted" in description
    assert "truncated" in description
    assert "degraded" in description
    assert "empty results list" in description


async def test_the_tool_is_off_unless_it_is_selected():
    """An agent that never queries the corpus should not pay its context tax."""
    assert _catalog_entry()["default_enabled"] is False
    assert _catalog_entry()["requires_tracker"] is False


# --- the operator decides whether the tool exists at all -------------------


def _list_tools_patches(offered):
    db = MagicMock()
    db.close = MagicMock()
    return (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db]),
        ),
        patch(
            "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
            return_value=[],
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration."
            "get_multi_by_account",
            return_value=[],
        ),
        patch(
            "preloop.models.crud.crud_account.get", return_value=MagicMock(meta_data={})
        ),
        patch.object(FastMCP, "list_tools", new=AsyncMock(return_value=offered)),
    )


async def test_a_preset_that_did_not_select_the_tool_is_not_offered_it():
    """The allow-list is the offer: an unselected tool is not in the list."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=["get_issue"])
    offered = [
        Tool(name=TOOL_NAME, description="Search sessions", parameters={}),
        Tool(name="get_issue", description="Get issue", parameters={}),
    ]

    patches = _list_tools_patches(offered)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await mcp.list_tools()

    assert {tool.name for tool in result} == {"get_issue"}


async def test_a_preset_that_selected_the_tool_is_offered_it_despite_default_off():
    """Selecting the tool on the flow is the opt in the default expects."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=[TOOL_NAME, "get_issue"])
    offered = [
        Tool(name=TOOL_NAME, description="Search sessions", parameters={}),
        Tool(name="get_issue", description="Get issue", parameters={}),
    ]

    patches = _list_tools_patches(offered)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await mcp.list_tools()

    assert {tool.name for tool in result} == {TOOL_NAME, "get_issue"}


def _call_tool_patches(available_names, *, policy=("allow", None, None)):
    db = MagicMock()
    db.close = MagicMock()
    available = [
        SimpleNamespace(name=name, description="", parameters={})
        for name in available_names
    ]
    return (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([db]),
        ),
        patch(
            "preloop.services.dynamic_fastmcp.kill_switch_service.tools_halted",
            return_value=False,
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration."
            "get_multi_by_account",
            return_value=[],
        ),
        patch(
            "preloop.services.policy_evaluator.evaluate_policy_async",
            new=AsyncMock(return_value=policy),
        ),
        available,
    )


async def test_a_policy_that_denies_the_tool_prevents_the_call():
    """The preset permits the search; a policy deny still stops it."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=[TOOL_NAME])
    db_patch, halt_patch, config_patch, policy_patch, available = _call_tool_patches(
        [TOOL_NAME], policy=("deny", None, "session search is off for this preset")
    )

    with (
        db_patch,
        halt_patch,
        config_patch,
        policy_patch,
        patch.object(mcp, "list_tools", new=AsyncMock(return_value=available)),
        patch.object(
            mcp.__class__.__bases__[0],
            "call_tool",
            new=AsyncMock(),
            create=True,
        ) as dispatch,
    ):
        result = await mcp.call_tool(TOOL_NAME, {"query": "migration"})

    assert "Access denied" in result.content[0].text
    assert "session search is off for this preset" in result.content[0].text
    dispatch.assert_not_called()


async def test_calling_the_tool_without_selecting_it_is_refused_at_the_boundary():
    """Naming a tool the preset does not have is denied before dispatch."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=["get_issue"])
    db_patch, halt_patch, config_patch, policy_patch, available = _call_tool_patches(
        ["get_issue"]
    )

    with (
        db_patch,
        halt_patch,
        config_patch,
        policy_patch,
        patch.object(mcp, "list_tools", new=AsyncMock(return_value=available)),
        patch.object(
            mcp.__class__.__bases__[0],
            "call_tool",
            new=AsyncMock(),
            create=True,
        ) as dispatch,
    ):
        result = await mcp.call_tool(TOOL_NAME, {"query": "migration"})

    assert "Access denied" in result.content[0].text
    dispatch.assert_not_called()


# --- the tool function's own guards ---------------------------------------


async def test_without_a_user_context_nothing_is_searched(mcp_server, monkeypatch):
    """No identity, no search, and no approval request either."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    search = MagicMock()
    approval = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: None,
    )
    monkeypatch.setattr("preloop.services.initialize_mcp.require_approval", approval)
    monkeypatch.setattr(
        "preloop.services.agent_session_search.search_for_agent", search
    )

    result = await tool.fn(query="migration")

    assert result.startswith("Error")
    approval.assert_not_awaited()
    search.assert_not_called()


async def test_an_approval_denial_stops_the_search(mcp_server, monkeypatch):
    """A workflow that declines the call is final: nothing is read."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    search = MagicMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct",
            user_id="user",
            runtime_principal_id="agent-1",
            api_key_id=None,
            managed_agent_id=None,
        ),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(False, "Denied by configured policy")),
    )
    monkeypatch.setattr(
        "preloop.services.agent_session_search.search_for_agent", search
    )

    result = await tool.fn(query="migration")

    assert result == "Denied by configured policy"
    search.assert_not_called()


async def test_the_scope_is_built_from_the_session_not_the_arguments(
    mcp_server, monkeypatch
):
    """An agent chooses what to search, never whose sessions."""
    tool = await mcp_server.get_tool(TOOL_NAME)
    answer = {"query": "migration", "scope": "own", "results": []}
    search = MagicMock(return_value=answer)
    session = MagicMock()
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(
            account_id="acct",
            user_id="user",
            runtime_principal_id="agent-1",
            api_key_id="key-1",
            managed_agent_id="agent-row-1",
        ),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        "preloop.services.agent_session_search.search_for_agent", search
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([session])
    )

    result = await tool.fn(query="migration", scope="account", limit=3)

    assert json.loads(result) == answer
    kwargs = search.call_args.kwargs
    assert kwargs["account_id"] == "acct"
    assert kwargs["runtime_principal_id"] == "agent-1"
    assert kwargs["subject_context"] == {
        "api_key_id": "key-1",
        "managed_agent_id": "agent-row-1",
    }
    assert kwargs["query"] == "migration"
    assert kwargs["scope"] == "account"
    assert kwargs["limit"] == 3
    session.close.assert_called_once()
