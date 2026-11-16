"""A rescanned upstream tool schema replaces the cached proxied wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool

from preloop.services.dynamic_fastmcp import DynamicFastMCP, _stale_schema_error
from preloop.services.dynamic_mcp_server import UserContext

pytestmark = pytest.mark.asyncio

ACCOUNT_ID = "00000000-0000-0000-0000-000000000002"
INTERNAL = "account_00000000_0000_0000_0000_000000000002_publish_release"

SCHEMA_V1 = {
    "type": "object",
    "properties": {
        "version": {"type": "string"},
        "change_uid": {"type": "string"},
        "long_description": {"type": "string"},
        "short_description": {"type": "string"},
    },
    "required": ["version", "change_uid", "long_description", "short_description"],
}

SCHEMA_V2 = {
    "type": "object",
    "properties": {
        "version_number": {"type": "string"},
        "version_date": {"type": "string"},
        "merge_requests": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
    },
    "required": ["version_number", "version_date", "description"],
}


def _context() -> UserContext:
    """Build a plain user context for the test account.

    Returns:
        A fresh UserContext (no cached tool list).
    """
    return UserContext(
        user_id="00000000-0000-0000-0000-000000000001",
        account_id=ACCOUNT_ID,
        username="test_user",
        has_tracker=False,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        tracker_types=[],
    )


def _row(schema: dict[str, Any]) -> list[tuple[Any, Any]]:
    """Return one (server, tool) discovery row carrying *schema*.

    Args:
        schema: The stored upstream input schema.

    Returns:
        The rows ``_get_proxied_tools_sync`` would return.
    """
    server = SimpleNamespace(id="srv-1", name="releases", tool_prefix=None)
    tool = SimpleNamespace(
        name="publish_release",
        description="Publish a release",
        input_schema=schema,
        annotations=None,
    )
    return [(server, tool)]


async def _list(mcp: DynamicFastMCP, schema: dict[str, Any]) -> None:
    """Run ``list_tools`` with the DB returning *schema* for the tool.

    Args:
        mcp: Server under test.
        schema: Stored input schema the scan wrote.
    """
    db = MagicMock()

    def _db() -> Iterator[MagicMock]:
        return iter([db])

    ctx = _context()
    mcp.set_user_context_provider(lambda: ctx)
    with (
        patch("preloop.services.dynamic_fastmcp.get_db", side_effect=_db),
        patch(
            "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
            return_value=_row(schema),
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration."
            "get_multi_by_account",
            return_value=[],
        ),
        patch(
            "preloop.models.crud.crud_account.get",
            return_value=MagicMock(meta_data={}),
        ),
    ):
        await mcp.list_tools()


async def _registered_params(mcp: DynamicFastMCP) -> set[str]:
    """Return the argument names the registered wrapper accepts.

    Args:
        mcp: Server under test.

    Returns:
        Property names of the registered wrapper's argument model.
    """
    tools = await FastMCP.list_tools(mcp, run_middleware=False)
    wrapper = next(t for t in tools if t.name == INTERNAL)
    return set(wrapper.parameters.get("properties", {})) - {"ctx"}


async def test_rescanned_schema_rebuilds_registered_wrapper() -> None:
    """After the stored schema changes, the callable accepts the new fields."""
    mcp = DynamicFastMCP(name="test")

    await _list(mcp, SCHEMA_V1)
    assert await _registered_params(mcp) == set(SCHEMA_V1["properties"])

    await _list(mcp, SCHEMA_V2)
    assert await _registered_params(mcp) == set(SCHEMA_V2["properties"])


async def test_unchanged_schema_keeps_registered_wrapper() -> None:
    """Listing again with the same schema does not rebuild the wrapper."""
    mcp = DynamicFastMCP(name="test")
    await _list(mcp, SCHEMA_V1)
    with patch.object(
        mcp, "_create_proxied_tool_wrapper", wraps=mcp._create_proxied_tool_wrapper
    ) as factory:
        await _list(mcp, dict(SCHEMA_V1))
    factory.assert_not_called()


def _listed(schema: dict[str, Any]) -> list[Tool]:
    """Return the client-facing tool list for the proxied tool.

    Args:
        schema: Stored input schema.

    Returns:
        A one-tool list as ``list_tools`` would expose it.
    """
    return [Tool(name="publish_release", description="", parameters=schema)]


def test_stale_schema_error_names_unknown_fields_and_rescan() -> None:
    """Arguments outside the stored schema get a rescan hint, not a bare error."""
    text = _stale_schema_error(
        "publish_release",
        {"version_number": "1.0.0", "version": "1.0.0"},
        _listed(SCHEMA_V1),
        "releases",
    )
    assert text is not None
    assert "version_number" in text
    assert "stale" in text
    assert "preloop mcp-servers scan releases" in text
    assert "change_uid" in text  # accepted arguments are listed


def test_stale_schema_error_silent_when_call_fits_schema() -> None:
    """A call within the stored schema, or a builtin tool, is untouched."""
    fits = {"version": "1", "justification": "release"}
    assert (
        _stale_schema_error("publish_release", fits, _listed(SCHEMA_V1), "releases")
        is None
    )
    assert (
        _stale_schema_error("publish_release", {"x": 1}, _listed(SCHEMA_V1), None)
        is None
    )


async def test_replay_after_schema_change_explains_instead_of_failing() -> None:
    """Approved arguments that no longer fit get an explicit refusal."""
    mcp = DynamicFastMCP(name="test")
    await _list(mcp, SCHEMA_V1)
    await _list(mcp, SCHEMA_V2)  # rescan while an approval was pending
    approved = {
        "version": "1",
        "change_uid": "c",
        "long_description": "l",
        "short_description": "s",
    }
    with (
        patch.object(mcp, "_halt_dispatch_denial", AsyncMock(return_value=None)),
        patch.object(mcp, "_record_attributed_refusal") as refusal,
    ):
        result = await mcp.call_registered_tool_without_policy(
            INTERNAL, approved, account_id=ACCOUNT_ID
        )
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert "changed after this call was approved" in text
    assert "publish_release" in text
    assert "version_number" in text  # newly required
    refusal.assert_called_once()


async def test_replay_with_fitting_arguments_dispatches() -> None:
    """Arguments that fit the current schema are not refused."""
    mcp = DynamicFastMCP(name="test")
    await _list(mcp, SCHEMA_V2)
    assert (
        mcp._replay_schema_error(
            INTERNAL,
            {"version_number": "2", "version_date": "d", "description": "x"},
        )
        is None
    )


def test_unregister_keeps_wrapper_owned_by_another_server() -> None:
    """Deleting a shadowed server leaves the owning server's wrapper."""
    mcp = DynamicFastMCP(name="test")
    mcp._registered_proxied_tools.add(INTERNAL)
    mcp._proxied_tool_servers["publish_release"] = "owner"
    removed = mcp.unregister_proxied_tools(ACCOUNT_ID, "shadowed", ["publish_release"])
    assert removed == 0
    assert INTERNAL in mcp._registered_proxied_tools
    assert mcp._proxied_tool_servers["publish_release"] == "owner"
