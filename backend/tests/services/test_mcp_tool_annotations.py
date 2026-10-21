"""MCP tool annotations on builtin and proxied tools (catalog listing prerequisite)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import Tool as McpTool
from mcp.types import ToolAnnotations

from preloop.api.endpoints.tools import BUILTIN_TOOLS
from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    upstream_tool_annotations,
    with_builtin_annotations,
)
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from preloop.services.mcp_tool_discovery import upstream_annotations
from preloop.tools.builtin_defs import BUILTIN_TOOL_ANNOTATIONS

pytestmark = pytest.mark.asyncio

HINT_KEYS = {
    "title",
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
}


def _context() -> UserContext:
    """Build a plain (non-flow) user context with a GitHub tracker.

    Returns:
        A UserContext for list_tools.
    """
    return UserContext(
        user_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        username="test_user",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        tracker_types=["github"],
    )


def _patches(offered: list[Tool], configs: list[Any] | None = None) -> list[Any]:
    """Patch the DB lookups list_tools performs.

    Args:
        offered: Tools the FastMCP base class returns.
        configs: Tool configuration rows for the account.

    Returns:
        The patch context managers.
    """
    db = MagicMock()

    def _db() -> Iterator[MagicMock]:
        return iter([db])

    return [
        patch("preloop.services.dynamic_fastmcp.get_db", side_effect=_db),
        patch(
            "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
            return_value=[],
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration."
            "get_multi_by_account",
            return_value=configs or [],
        ),
        patch(
            "preloop.models.crud.crud_account.get",
            return_value=MagicMock(meta_data={}),
        ),
        patch.object(FastMCP, "list_tools", new=AsyncMock(return_value=offered)),
    ]


async def _list(mcp: DynamicFastMCP, patches: list[Any]) -> list[Tool]:
    """Run list_tools under the given patches.

    Args:
        mcp: Server under test.
        patches: Patch context managers from ``_patches``.

    Returns:
        The tools list_tools returned.
    """
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        return await mcp.list_tools()


async def test_every_registered_builtin_tool_declares_annotations() -> None:
    """Each tool initialize_mcp registers, and each REST builtin, is annotated."""
    server = initialize_mcp_with_tools()
    registered = {
        t.name for t in await FastMCP.list_tools(server, run_middleware=False)
    }
    catalog = {t["name"] for t in BUILTIN_TOOLS}

    assert registered, "no builtin tools registered"
    assert registered - set(BUILTIN_TOOL_ANNOTATIONS) == set()
    assert catalog - set(BUILTIN_TOOL_ANNOTATIONS) == set()
    assert set(BUILTIN_TOOL_ANNOTATIONS) - (registered | catalog) == set()
    for name, hints in BUILTIN_TOOL_ANNOTATIONS.items():
        assert set(hints) == HINT_KEYS, name
        ToolAnnotations.model_validate(hints)


@pytest.mark.parametrize(
    "name",
    [n for n, h in BUILTIN_TOOL_ANNOTATIONS.items() if h["readOnlyHint"]],
)
async def test_read_only_tools_are_not_destructive(name: str) -> None:
    """Read-only hints never claim destructive or non-idempotent behaviour."""
    hints = BUILTIN_TOOL_ANNOTATIONS[name]
    assert hints["destructiveHint"] is False
    assert hints["idempotentHint"] is True


async def test_hint_values_match_tool_behaviour() -> None:
    """Spot-check the hints the catalog reviewers read."""
    hints = BUILTIN_TOOL_ANNOTATIONS
    for name in ("get_issue", "search_issues", "get_pull_request", "get_artifact"):
        assert hints[name]["readOnlyHint"] is True, name
    for name in (
        "create_issue",
        "update_issue",
        "add_comment",
        "create_pull_request",
        "update_pull_request",
        "run_flow",
        "deposit_artifact",
    ):
        assert hints[name]["readOnlyHint"] is False, name
        assert hints[name]["destructiveHint"] is False, name
    assert hints["request_approval"]["openWorldHint"] is True
    assert hints["list_sessions"]["openWorldHint"] is False


async def test_tools_list_wire_format_carries_annotations() -> None:
    """The MCP tools/list payload exposes title and all four hints."""
    tool = with_builtin_annotations(
        Tool(name="get_issue", description="Get issue", parameters={})
    )
    wire = tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)

    assert wire["title"] == "Get issue"
    assert wire["annotations"] == BUILTIN_TOOL_ANNOTATIONS["get_issue"]


async def test_list_tools_annotates_builtins() -> None:
    """DynamicFastMCP.list_tools returns builtin tools with their annotations."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = _context
    offered = [
        Tool(name="get_issue", description="Get issue", parameters={}),
        Tool(name="create_issue", description="Create issue", parameters={}),
    ]

    result = {t.name: t for t in await _list(mcp, _patches(offered))}

    assert result["get_issue"].annotations is not None
    assert result["get_issue"].annotations.readOnlyHint is True
    assert result["create_issue"].annotations is not None
    assert result["create_issue"].annotations.readOnlyHint is False
    assert result["create_issue"].annotations.destructiveHint is False


async def test_justification_injection_keeps_annotations() -> None:
    """Rebuilding a tool to add the justification field keeps its annotations."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = _context
    offered = [Tool(name="get_issue", description="Get issue", parameters={})]
    config = SimpleNamespace(
        tool_name="get_issue",
        tool_source="builtin",
        justification_mode="required",
        is_enabled=True,
        managed_agent_id=None,
    )

    result = await _list(mcp, _patches(offered, [config]))

    (tool,) = result
    assert "justification" in tool.parameters["properties"]
    assert tool.annotations is not None
    assert tool.annotations.title == "Get issue"
    assert tool.title == "Get issue"


async def test_existing_annotations_are_not_overwritten() -> None:
    """A tool that already carries annotations keeps them."""
    own = ToolAnnotations(title="Mine", readOnlyHint=False)
    tool = Tool(name="get_issue", parameters={}, annotations=own)

    assert with_builtin_annotations(tool).annotations is own


async def test_unknown_tool_is_left_unannotated() -> None:
    """Names without declared annotations pass through untouched."""
    tool = Tool(name="not_a_builtin", parameters={})

    assert with_builtin_annotations(tool) is tool


async def test_discovery_stores_upstream_annotations_unchanged() -> None:
    """Discovery keeps every upstream hint, unknown keys included."""
    upstream = McpTool(
        name="delete_repo",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations.model_validate(
            {"title": "Delete repo", "destructiveHint": True, "x-vendor": 1}
        ),
    )

    assert upstream_annotations(upstream) == {
        "title": "Delete repo",
        "destructiveHint": True,
        "x-vendor": 1,
    }
    assert upstream_annotations(McpTool(name="t", inputSchema={})) is None
    assert upstream_annotations(MagicMock()) is None


async def test_proxied_tool_annotations_pass_through() -> None:
    """Stored upstream annotations are re-exposed as sent; none stays none."""
    row = SimpleNamespace(
        name="delete_repo",
        annotations={"destructiveHint": True, "openWorldHint": True},
    )

    rebuilt = upstream_tool_annotations(row)

    assert rebuilt is not None
    assert rebuilt.model_dump(by_alias=True, exclude_none=True) == {
        "destructiveHint": True,
        "openWorldHint": True,
    }
    assert upstream_tool_annotations(SimpleNamespace(annotations=None)) is None
    assert (
        upstream_tool_annotations(
            SimpleNamespace(name="x", annotations={"readOnlyHint": "nope"})
        )
        is None
    )


async def test_list_tools_passes_proxied_annotations_through() -> None:
    """A proxied tool in tools/list carries its upstream annotations."""
    mcp = DynamicFastMCP("test-mcp")
    mcp._user_context_provider = _context
    server = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        name="upstream",
        tool_prefix=None,
    )
    row = SimpleNamespace(
        name="delete_repo",
        description="Delete a repository",
        input_schema={"type": "object", "properties": {}},
        annotations={"title": "Delete repo", "destructiveHint": True},
    )
    internal = "account_00000000_0000_0000_0000_000000000002_delete_repo"
    offered = [Tool(name=internal, parameters={})]
    patches = _patches(offered)
    patches[1] = patch(
        "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
        return_value=[(server, row)],
    )

    result = {t.name: t for t in await _list(mcp, patches)}

    proxied = result["delete_repo"]
    assert proxied.annotations is not None
    assert proxied.annotations.model_dump(by_alias=True, exclude_none=True) == {
        "title": "Delete repo",
        "destructiveHint": True,
    }
