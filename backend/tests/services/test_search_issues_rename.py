"""Tests for search -> search_issues tool rename and alias compatibility (#1044)."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool

from preloop.api.endpoints.tools import BUILTIN_TOOLS
from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from preloop.services.policy_evaluator import evaluate_policy
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    is_tool_enabled_for_subject,
    set_subject_governance,
)

pytestmark = pytest.mark.asyncio


def _flow_context(*, allowed=None, has_tracker=True):
    return UserContext(
        user_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        username="test_user",
        has_tracker=has_tracker,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        tracker_types=["github"],
        flow_execution_id="flow-1" if allowed is not None else None,
        allowed_flow_tools=list(allowed) if allowed is not None else None,
    )


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
            "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
            return_value=[],
        ),
        patch(
            "preloop.models.crud.crud_account.get",
            return_value=MagicMock(meta_data={}),
        ),
        patch.object(FastMCP, "list_tools", new=AsyncMock(return_value=offered)),
    )


async def test_search_issues_registered_in_builtin_tools():
    """search_issues is present, tracker-dependent, and default enabled; search is alias."""
    search_issues_entry = next(
        (t for t in BUILTIN_TOOLS if t["name"] == "search_issues"), None
    )
    assert search_issues_entry is not None
    assert search_issues_entry["requires_tracker"] is True
    assert search_issues_entry.get("default_enabled", True) is True
    assert "Search issues and comments" in search_issues_entry["description"]
    assert "Read-only" in search_issues_entry["description"]

    search_alias = next((t for t in BUILTIN_TOOLS if t["name"] == "search"), None)
    assert search_alias is not None
    assert search_alias["requires_tracker"] is True
    assert search_alias.get("default_enabled", True) is False
    assert "Deprecated" in search_alias["description"]
    assert "0.18.0" in search_alias["description"]


async def test_fresh_account_shows_search_issues_and_not_search():
    """tools/list from a fresh account shows search_issues and not search unless named (#1044)."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(has_tracker=True)

    offered = [
        Tool(name="search_issues", description="Search issues", parameters={}),
        Tool(name="search", description="Search alias", parameters={}),
        Tool(name="get_issue", description="Get issue", parameters={}),
    ]

    patches = _list_tools_patches(offered)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await mcp.list_tools()

    tool_names = {t.name for t in result}
    assert "search_issues" in tool_names
    assert "get_issue" in tool_names
    # search is default_enabled: False, so absent on fresh account
    assert "search" not in tool_names


async def test_flow_naming_search_alias_retains_search_and_search_issues():
    """Existing flows that name search allow-list keep working via alias (#1044)."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = lambda: _flow_context(allowed=["search"])

    offered = [
        Tool(name="search_issues", description="Search issues", parameters={}),
        Tool(name="search", description="Search alias", parameters={}),
        Tool(name="get_issue", description="Get issue", parameters={}),
    ]

    patches = _list_tools_patches(offered)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await mcp.list_tools()

    tool_names = {t.name for t in result}
    assert "search" in tool_names
    assert "search_issues" in tool_names
    assert "get_issue" not in tool_names


async def test_fastmcp_registers_both_tools_with_same_signature():
    """initialize_mcp_with_tools registers search_issues and deprecated search alias."""
    server = initialize_mcp_with_tools()
    search_issues_tool = await server.get_tool("search_issues")
    search_tool = await server.get_tool("search")

    assert search_issues_tool is not None
    assert search_tool is not None

    assert "Deprecated" in search_tool.description
    assert "search_issues" in search_tool.description

    # Parameter signatures match
    sig_issues = inspect.signature(search_issues_tool.fn)
    sig_search = inspect.signature(search_tool.fn)
    assert set(sig_issues.parameters.keys()) == set(sig_search.parameters.keys())


async def test_search_override_blocks_search_issues_call():
    """``tool_enabled_overrides: {"search": false}`` denies a search_issues call.

    The renamed tool is default-enabled, so an override stored under the
    legacy name has to apply through TOOL_NAME_ALIASES or the disable is
    silently bypassed.
    """
    subject_context = {"api_key_id": "key-search", "managed_agent_id": None}
    meta = set_subject_governance(
        {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id="key-search",
        config={"tool_enabled_overrides": {"search": False}},
    )
    assert (
        is_tool_enabled_for_subject(
            meta, tool_name="search_issues", subject_context=subject_context
        )
        is False
    )
    # Same scope, both names set: a disable still wins over an enable.
    conflict = set_subject_governance(
        {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id="key-search",
        config={"tool_enabled_overrides": {"search": False, "search_issues": True}},
    )
    assert (
        is_tool_enabled_for_subject(
            conflict, tool_name="search_issues", subject_context=subject_context
        )
        is False
    )
    # The other direction: disabling the canonical name blocks the alias.
    reverse = set_subject_governance(
        {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id="key-search",
        config={"tool_enabled_overrides": {"search_issues": False}},
    )
    assert (
        is_tool_enabled_for_subject(
            reverse, tool_name="search", subject_context=subject_context
        )
        is False
    )

    account = MagicMock()
    account.meta_data = meta
    with patch(
        "preloop.services.policy_evaluator.crud_account.get",
        return_value=account,
    ):
        action, _approval_id, description = evaluate_policy(
            db=MagicMock(),
            tool_name="search_issues",
            tool_args={"query": "bug"},
            account_id=uuid4(),
            subject_context=subject_context,
        )
    assert action == "deny"
    assert description is not None
    assert "disabled" in description.lower()
