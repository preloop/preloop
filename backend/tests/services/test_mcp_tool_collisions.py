"""Same-named MCP tools across servers (#1135).

First-wins by the oldest active server, shadowed marking with warnings and
audit events, and the optional explicit ``tool_prefix``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastmcp.tools import Tool
from mcp import types
from pydantic import ValidationError

from preloop.models.crud import crud_mcp_tool
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.mcp_server import MCPServerCreate, MCPServerUpdate
from preloop.services import mcp_tool_collisions as collisions
from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    _resolve_proxied_tool_server,
)
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.mcp_client_pool import UpstreamToolContent
from preloop.services.mcp_tool_discovery import (
    _get_proxied_tools_sync,
    scan_mcp_server_tools,
)

MOD = "preloop.services.dynamic_fastmcp"
T0 = datetime(2026, 10, 1, 12, 0, 0)


def _server(db, user, name, *, age_minutes=0, prefix=None, server_id=None, tools=()):
    server = MCPServer(
        name=name,
        url=f"http://{name}.example.test/mcp",
        transport="http-streaming",
        auth_type="none",
        account_id=user.account_id,
        status="active",
        tool_prefix=prefix,
    )
    if server_id is not None:
        server.id = server_id
    db.add(server)
    db.commit()
    server.created_at = T0 + timedelta(minutes=age_minutes)
    for tool_name in tools:
        db.add(
            MCPTool(
                mcp_server_id=server.id,
                name=tool_name,
                description=f"{tool_name} on {name}",
                input_schema={"type": "object", "properties": {}},
                discovered_at="2026-10-01T00:00:00Z",
            )
        )
    db.commit()
    db.refresh(server)
    return server


def _tool(db, server, name):
    return crud_mcp_tool.get_by_server_and_name(db, server_id=server.id, name=name)


# ---------------------------------------------------------------------------
# Tiebreak and first-wins
# ---------------------------------------------------------------------------


def test_resolver_tiebreak_is_oldest_then_lowest_id(db_session, test_user):
    account_id = str(test_user.account_id)
    newer = _server(db_session, test_user, "newer", age_minutes=5, tools=["read_scope"])
    older = _server(db_session, test_user, "older", age_minutes=0, tools=["read_scope"])
    assert _resolve_proxied_tool_server(db_session, account_id, "read_scope").id == (
        older.id
    )

    # Same created_at: the lower id owns the name, whatever the insert order.
    high = _server(
        db_session,
        test_user,
        "tie-high",
        age_minutes=-10,
        server_id=UUID("ffffffff-0000-0000-0000-000000000000"),
        tools=["tie_tool"],
    )
    low = _server(
        db_session,
        test_user,
        "tie-low",
        age_minutes=-10,
        server_id=UUID("00000000-0000-0000-0000-000000000001"),
        tools=["tie_tool"],
    )
    assert _resolve_proxied_tool_server(db_session, account_id, "tie_tool").id == low.id
    assert high.id != low.id and newer.id != older.id


def test_listing_shows_a_name_once_from_the_owner(db_session, test_user):
    older = _server(db_session, test_user, "a", age_minutes=0, tools=["read_scope"])
    _server(db_session, test_user, "b", age_minutes=5, tools=["read_scope", "only_b"])
    rows = _get_proxied_tools_sync(str(test_user.account_id), db_session)
    names = [(server.name, tool.name) for server, tool in rows]
    assert names.count(("a", "read_scope")) == 1
    assert ("b", "read_scope") not in names
    assert ("b", "only_b") in names
    assert [s for s, t in rows if t.name == "read_scope"][0].id == older.id


def test_disabled_owner_tool_hides_the_name_instead_of_handing_it_over(
    db_session, test_user
):
    account_id = str(test_user.account_id)
    older = _server(db_session, test_user, "a", age_minutes=0, tools=["read_scope"])
    _server(db_session, test_user, "b", age_minutes=5, tools=["read_scope"])
    db_session.add(
        ToolConfiguration(
            account_id=test_user.account_id,
            tool_name="read_scope",
            tool_source="mcp",
            mcp_server_id=older.id,
            is_enabled=False,
        )
    )
    db_session.commit()
    assert _resolve_proxied_tool_server(db_session, account_id, "read_scope") is None
    rows = _get_proxied_tools_sync(account_id, db_session)
    assert all(tool.name != "read_scope" for _, tool in rows)


# ---------------------------------------------------------------------------
# Shadowed marking, un-shadowing, audit
# ---------------------------------------------------------------------------


def test_recompute_marks_newer_server_and_groups_one_collision_set(
    db_session, test_user
):
    a = _server(db_session, test_user, "a", age_minutes=0, tools=["x", "y", "z"])
    b = _server(db_session, test_user, "b", age_minutes=5, tools=["x", "y", "w"])
    changes = collisions.recompute_shadowing(db_session, str(test_user.account_id))

    assert len(changes.shadowed) == 1
    collision = changes.shadowed[0].as_audit_value()
    assert collision == {
        "owner_server_id": str(a.id),
        "owner_server_name": "a",
        "shadowed_server_id": str(b.id),
        "shadowed_server_name": "b",
        "tool_names": ["x", "y"],
    }
    assert _tool(db_session, b, "x").shadowed is True
    assert _tool(db_session, b, "w").shadowed is False
    assert _tool(db_session, a, "x").shadowed is False

    # Idempotent: nothing changes, nothing to audit.
    again = collisions.recompute_shadowing(db_session, str(test_user.account_id))
    assert again.shadowed == [] and again.unshadowed == []


@pytest.mark.parametrize("how", ["delete", "disable"])
def test_owner_removed_unshadows_the_next_oldest(db_session, test_user, how):
    a = _server(db_session, test_user, "a", age_minutes=0, tools=["x"])
    b = _server(db_session, test_user, "b", age_minutes=5, tools=["x"])
    c = _server(db_session, test_user, "c", age_minutes=9, tools=["x"])
    collisions.recompute_shadowing(db_session, str(test_user.account_id))
    assert _tool(db_session, b, "x").shadowed and _tool(db_session, c, "x").shadowed

    if how == "delete":
        db_session.delete(a)
    else:
        a.status = "disabled"
    db_session.commit()
    changes = collisions.recompute_shadowing(db_session, str(test_user.account_id))

    assert _tool(db_session, b, "x").shadowed is False
    assert _tool(db_session, c, "x").shadowed is True
    assert changes.unshadowed == [
        {"server_id": str(b.id), "server_name": "b", "tool_names": ["x"]}
    ]
    assert (
        _resolve_proxied_tool_server(db_session, str(test_user.account_id), "x").id
        == b.id
    )


def test_one_audit_event_per_collision_set(db_session, test_user, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "preloop.utils.audit.log_config_change",
        lambda db, **kwargs: calls.append(kwargs),
    )
    a = _server(db_session, test_user, "a", age_minutes=0, tools=["x", "y"])
    b = _server(db_session, test_user, "b", age_minutes=5, tools=["x", "y"])
    c = _server(db_session, test_user, "c", age_minutes=9, tools=["y"])
    collisions.recompute_and_audit(db_session, str(test_user.account_id), test_user)

    assert len(calls) == 2
    assert {call["config_type"] for call in calls} == {"mcp_tool_collision"}
    assert {call["action"] for call in calls} == {"shadowed"}
    by_shadowed = {call["new_value"]["shadowed_server_id"]: call for call in calls}
    assert by_shadowed[str(b.id)]["new_value"]["tool_names"] == ["x", "y"]
    assert by_shadowed[str(c.id)]["new_value"]["tool_names"] == ["y"]
    assert by_shadowed[str(c.id)]["new_value"]["owner_server_id"] == str(a.id)

    calls.clear()
    db_session.delete(a)
    db_session.commit()
    collisions.recompute_and_audit(db_session, str(test_user.account_id), test_user)
    # b takes over x and y. c's y stays shadowed (now by b): no flag
    # changed for it, so the only event is b's un-shadowing.
    assert [call["action"] for call in calls] == ["unshadowed"]
    assert calls[0]["new_value"] == {
        "server_id": str(b.id),
        "server_name": "b",
        "tool_names": ["x", "y"],
    }


@pytest.mark.asyncio
async def test_scan_marks_shadowed_and_warns(db_session, test_user, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "preloop.utils.audit.log_config_change",
        lambda db, **kwargs: calls.append(kwargs),
    )
    _server(db_session, test_user, "older", age_minutes=0, tools=["read_scope"])
    newer = _server(db_session, test_user, "newer", age_minutes=5)
    client = MagicMock()
    client.list_tools = AsyncMock(
        return_value=[
            SimpleNamespace(name="read_scope", description="d", inputSchema={}),
            SimpleNamespace(name="list_scopes", description="d", inputSchema={}),
        ]
    )
    monkeypatch.setattr(
        "preloop.services.mcp_tool_discovery.get_mcp_client_pool",
        lambda: MagicMock(get_client=AsyncMock(return_value=client)),
    )
    await scan_mcp_server_tools(newer.id, db_session, user=test_user)

    assert _tool(db_session, newer, "read_scope").shadowed is True
    assert _tool(db_session, newer, "list_scopes").shadowed is False
    assert collisions.server_warnings(db_session, newer) == [
        "Tool 'read_scope' on MCP server 'newer' is shadowed by MCP server "
        "'older', which was added earlier and exposes the same name. Agents "
        "see and call only the tool from 'older'. Set a tool prefix on this "
        "server to expose both."
    ]
    assert [call["action"] for call in calls] == ["shadowed"]


def test_runtime_session_names_skip_shadowed_and_use_prefix(db_session, test_user):
    a = _server(db_session, test_user, "a", age_minutes=0, tools=["x"])
    b = _server(db_session, test_user, "b", age_minutes=5, tools=["x", "only_b"])
    c = _server(db_session, test_user, "c", age_minutes=9, prefix="crm", tools=["x"])
    collisions.recompute_shadowing(db_session, str(test_user.account_id))
    names = crud_mcp_tool.get_tool_names_by_server_ids(db_session, [b.id, c.id])
    assert sorted(names) == ["crm_x", "only_b"]
    assert crud_mcp_tool.get_tool_names_by_server_ids(db_session, [a.id]) == ["x"]


# ---------------------------------------------------------------------------
# Explicit prefix
# ---------------------------------------------------------------------------


def test_prefix_exposes_both_and_routes_by_exposed_name(db_session, test_user):
    account_id = str(test_user.account_id)
    a = _server(db_session, test_user, "a", age_minutes=0, tools=["read_scope"])
    b = _server(
        db_session, test_user, "b", age_minutes=5, prefix="crm", tools=["read_scope"]
    )
    changes = collisions.recompute_shadowing(db_session, account_id)
    assert changes.shadowed == []
    assert collisions.server_warnings(db_session, b) == []

    rows = _get_proxied_tools_sync(account_id, db_session)
    exposed = sorted(
        collisions.exposed_tool_name(server.tool_prefix, tool.name)
        for server, tool in rows
    )
    assert exposed == ["crm_read_scope", "read_scope"]
    assert _resolve_proxied_tool_server(db_session, account_id, "read_scope").id == (
        a.id
    )
    assert _resolve_proxied_tool_server(
        db_session, account_id, "crm_read_scope"
    ).id == (b.id)


def test_prefix_configuration_is_keyed_by_exposed_name(db_session, test_user):
    account_id = str(test_user.account_id)
    _server(db_session, test_user, "a", age_minutes=0, tools=["read_scope"])
    b = _server(
        db_session, test_user, "b", age_minutes=5, prefix="crm", tools=["read_scope"]
    )
    db_session.add(
        ToolConfiguration(
            account_id=test_user.account_id,
            tool_name="crm_read_scope",
            tool_source="mcp",
            mcp_server_id=b.id,
            is_enabled=False,
        )
    )
    db_session.commit()
    rows = _get_proxied_tools_sync(account_id, db_session)
    assert [(s.name, t.name) for s, t in rows] == [("a", "read_scope")]
    assert (
        _resolve_proxied_tool_server(db_session, account_id, "crm_read_scope") is None
    )
    assert _resolve_proxied_tool_server(db_session, account_id, "read_scope")


@pytest.mark.parametrize("prefix", ["crm", "crm_2", "a" * 32, "_x"])
def test_valid_prefixes(prefix):
    assert MCPServerCreate(name="n", url="http://u", tool_prefix=prefix).tool_prefix
    assert collisions.validate_tool_prefix(prefix) == prefix


@pytest.mark.parametrize("prefix", ["CRM", "crm-2", "crm.2", "a" * 33, "crm x", "é"])
def test_invalid_prefixes_are_rejected(prefix):
    with pytest.raises(ValidationError):
        MCPServerCreate(name="n", url="http://u", tool_prefix=prefix)
    with pytest.raises(ValueError):
        collisions.validate_tool_prefix(prefix)


def test_empty_prefix_clears():
    assert MCPServerUpdate(tool_prefix="").tool_prefix is None
    assert MCPServerUpdate(tool_prefix=None).tool_prefix is None


def test_resulting_name_over_128_is_warned_and_not_listed(db_session, test_user):
    long_tool = "t" * 121
    server = _server(
        db_session, test_user, "a", prefix="toolong", tools=[long_tool, "ok"]
    )
    rows = _get_proxied_tools_sync(str(test_user.account_id), db_session)
    assert [t.name for _, t in rows] == ["ok"]
    warning = collisions.server_warnings(db_session, server)
    assert warning == [
        f"Tool 'toolong_{long_tool}' on MCP server 'a' is not exposed: the name "
        "is not a valid MCP tool name (1 to 128 characters from A-Z, a-z, 0-9, "
        "'_', '-', '.')."
    ]


@pytest.mark.asyncio
async def test_prefixed_call_reaches_upstream_with_the_upstream_name(
    db_session, test_user, monkeypatch
):
    """A call to ``crm_read_scope`` routes to the prefixed server as ``read_scope``,
    and a call to the shadowed bare name goes to the owner."""
    a = _server(db_session, test_user, "a", age_minutes=0, tools=["read_scope"])
    b = _server(
        db_session, test_user, "b", age_minutes=5, prefix="crm", tools=["read_scope"]
    )
    user_context = UserContext(
        user_id=str(uuid4()),
        account_id=str(test_user.account_id),
        username="t",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )
    mcp = DynamicFastMCP("collision-test")
    mcp.set_user_context_provider(lambda: user_context)
    client = MagicMock()
    client.call_tool = AsyncMock(
        return_value=UpstreamToolContent([types.TextContent(type="text", text="ok")])
    )
    pool = MagicMock(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(f"{MOD}.get_db", lambda: iter([MagicMock()]))
    monkeypatch.setattr(
        f"{MOD}.kill_switch_service.tools_halted", lambda db, account_id: False
    )
    monkeypatch.setattr(
        f"{MOD}.crud_tool_configuration.get_multi_by_account", lambda *a, **k: []
    )
    monkeypatch.setattr(
        f"{MOD}._resolve_proxied_tool_server",
        lambda db, account_id, name: _resolve_proxied_tool_server(
            db_session, account_id, name
        ),
    )
    monkeypatch.setattr(f"{MOD}.get_mcp_client_pool", lambda: pool)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "preloop.models.db.session.get_async_db_session", lambda: session
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        AsyncMock(return_value=("allow", None, None)),
    )
    monkeypatch.setattr(
        "preloop.services.approval_helper.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        mcp,
        "list_tools",
        AsyncMock(
            return_value=[
                Tool(name="read_scope", description="d", parameters={}),
                Tool(name="crm_read_scope", description="d", parameters={}),
            ]
        ),
    )
    monkeypatch.setattr(mcp, "_halt_dispatch_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp, "_persist_tool_call_activity", MagicMock())
    plugin_manager = MagicMock()
    plugin_manager.get_service.return_value = MagicMock()
    monkeypatch.setattr(
        "preloop.plugins.base.get_plugin_manager", lambda: plugin_manager
    )
    safe = user_context.account_id.replace("-", "_")
    for name in ("read_scope", "crm_read_scope"):
        wrapper = mcp._create_proxied_tool_wrapper(
            tool_name=name,
            account_id=user_context.account_id,
            description="d",
            input_schema={"properties": {"scope": {"type": "string"}}},
        )
        mcp.tool()(wrapper)
        mcp._registered_proxied_tools.add(f"account_{safe}_{name}")
        mcp._proxied_tool_servers[name] = "listed"

    result = await mcp.call_tool("crm_read_scope", {"scope": "daily"})
    assert not result.is_error, result.content[0].text
    assert pool.get_client.await_args.kwargs["server_id"] == str(b.id)
    assert client.call_tool.await_args.args[0] == "read_scope"

    result = await mcp.call_tool("read_scope", {"scope": "daily"})
    assert not result.is_error, result.content[0].text
    assert pool.get_client.await_args.kwargs["server_id"] == str(a.id)
    assert client.call_tool.await_args.args[0] == "read_scope"
