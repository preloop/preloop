"""Call-time server routing (#1366) and audit rows for refusals (#1367).

Also covers the zero-tools warning on runtime-session token mint and the
usage-row argument hash under reference-only logging (#1368).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp.tools import Tool
from mcp import types

from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool
from preloop.services.dynamic_fastmcp import (
    AUDIT_TOOL_CALL_DECLINED,
    DynamicFastMCP,
    _resolve_proxied_tool_server,
)
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.mcp_client_pool import UpstreamToolContent
from preloop.services.policy.schema import SensitiveDataConfig
from preloop.services.sensitive_data import reference, storage

pytestmark = pytest.mark.asyncio

MOD = "preloop.services.dynamic_fastmcp"
SECRET_DID = "did:example:owner1"


@pytest.fixture
def user_context():
    return UserContext(
        user_id=str(uuid4()),
        account_id=str(uuid4()),
        username="testuser",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )


@pytest.fixture(autouse=True)
def clean_storage_cache():
    storage.invalidate_cache()
    yield
    storage.invalidate_cache()


def _audit_plugin(monkeypatch):
    audit_service = MagicMock()
    plugin_manager = MagicMock()
    plugin_manager.get_service.side_effect = lambda key: (
        audit_service if key == "audit_service" else None
    )
    monkeypatch.setattr(
        "preloop.plugins.base.get_plugin_manager", lambda: plugin_manager
    )
    return audit_service


def _server(server_id: str, name: str = "verify-source"):
    return SimpleNamespace(
        id=server_id,
        name=name,
        url="http://upstream.example.test/mcp",
        auth_type="bearer",
        auth_config={"token": f"token-for-{server_id}"},
        transport="http-streaming",
    )


# ---------------------------------------------------------------------------
# F2: the wrapper resolves its server on every call
# ---------------------------------------------------------------------------


def _proxied_setup(monkeypatch, user_context, proxied_rows):
    mcp = DynamicFastMCP("routing-test")
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
        "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
        lambda account_id, db: proxied_rows(),
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
            return_value=[Tool(name="read_scope", description="d", parameters={})]
        ),
    )
    monkeypatch.setattr(mcp, "_halt_dispatch_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp, "_persist_tool_call_activity", MagicMock())
    _audit_plugin(monkeypatch)

    wrapper = mcp._create_proxied_tool_wrapper(
        tool_name="read_scope",
        account_id=user_context.account_id,
        description="Read a scope",
        input_schema={"properties": {"scope": {"type": "string"}}},
    )
    internal = f"account_{user_context.account_id.replace('-', '_')}_read_scope"
    mcp.tool()(wrapper)
    mcp._registered_proxied_tools.add(internal)
    mcp._proxied_tool_servers["read_scope"] = "old"
    return mcp, pool, wrapper


async def test_wrapper_source_has_no_baked_server_id(monkeypatch, user_context):
    _, _, wrapper = _proxied_setup(monkeypatch, user_context, lambda: [])
    assert "server_id" not in wrapper.__globals__
    assert wrapper.__globals__["_resolve_proxied_tool_server"] is (
        _resolve_proxied_tool_server
    )


async def test_call_routes_to_recreated_server_without_restart(
    monkeypatch, user_context
):
    """Delete + recreate: the same registered wrapper reaches the new id."""
    tool = SimpleNamespace(name="read_scope")
    current = {"rows": [(_server("old-id"), tool)]}
    mcp, pool, _ = _proxied_setup(monkeypatch, user_context, lambda: current["rows"])

    first = await mcp.call_tool("read_scope", {"scope": "daily"})
    assert not first.is_error
    assert pool.get_client.await_args.kwargs["server_id"] == "old-id"

    # The old row is deleted and a new one with the same name is created.
    current["rows"] = [(_server("new-id"), tool)]
    second = await mcp.call_tool("read_scope", {"scope": "daily"})
    assert not second.is_error, second.content[0].text
    kwargs = pool.get_client.await_args.kwargs
    assert kwargs["server_id"] == "new-id"
    assert kwargs["auth_config"] == {"token": "token-for-new-id"}


async def test_second_credential_in_account_reaches_new_server(
    monkeypatch, user_context
):
    tool = SimpleNamespace(name="read_scope")
    mcp, pool, _ = _proxied_setup(
        monkeypatch, user_context, lambda: [(_server("new-id"), tool)]
    )
    other = UserContext(
        user_id=str(uuid4()),
        account_id=user_context.account_id,
        username="other",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
        runtime_session_id=str(uuid4()),
    )
    mcp.set_user_context_provider(lambda: other)
    result = await mcp.call_tool("read_scope", {"scope": "daily"})
    assert not result.is_error
    assert pool.get_client.await_args.kwargs["server_id"] == "new-id"


async def test_deleted_server_without_replacement_is_a_clean_error(
    monkeypatch, user_context
):
    mcp, pool, _ = _proxied_setup(monkeypatch, user_context, lambda: [])
    result = await mcp.call_tool("read_scope", {"scope": "daily"})
    assert result.is_error
    assert "no active MCP server provides tool 'read_scope'" in result.content[0].text
    pool.get_client.assert_not_awaited()


def test_resolver_reads_current_rows_after_delete_and_recreate(db_session, test_user):
    """Database-level: the resolver follows the row, not a cached id."""
    account_id = str(test_user.account_id)

    def make(name="verify-source"):
        server = MCPServer(
            name=name,
            url="http://localhost:9000/mcp",
            transport="http-streaming",
            auth_type="bearer",
            auth_config={"token": "T"},
            account_id=test_user.account_id,
            status="active",
        )
        db_session.add(server)
        db_session.commit()
        db_session.add(
            MCPTool(
                mcp_server_id=server.id,
                name="read_scope",
                description="d",
                input_schema={"type": "object", "properties": {}},
                discovered_at="2026-01-01T00:00:00Z",
            )
        )
        db_session.commit()
        db_session.refresh(server)
        return server

    first = make()
    assert _resolve_proxied_tool_server(db_session, account_id, "read_scope").id == (
        first.id
    )
    db_session.delete(first)
    db_session.commit()
    assert _resolve_proxied_tool_server(db_session, account_id, "read_scope") is None
    second = make()
    assert _resolve_proxied_tool_server(db_session, account_id, "read_scope").id == (
        second.id
    )
    assert _resolve_proxied_tool_server(db_session, str(uuid4()), "read_scope") is None


async def test_unregister_proxied_tools_removes_wrapper(monkeypatch, user_context):
    mcp, _, _ = _proxied_setup(monkeypatch, user_context, lambda: [])
    internal = f"account_{user_context.account_id.replace('-', '_')}_read_scope"
    removed = mcp.unregister_proxied_tools(
        user_context.account_id, "old", ["read_scope", "never_registered"]
    )
    assert removed == 1
    assert internal not in mcp._registered_proxied_tools
    assert "read_scope" not in mcp._proxied_tool_servers
    assert internal not in {
        t.name for t in await DynamicFastMCP.__mro__[1].list_tools(mcp)
    }


# ---------------------------------------------------------------------------
# F4b: every refusal before dispatch writes a declined audit row
# ---------------------------------------------------------------------------


def _refusal_setup(monkeypatch, user_context, *, configs=(), tools=("safe_tool",)):
    mcp = DynamicFastMCP("refusal-test")
    mcp.set_user_context_provider(lambda: user_context)
    monkeypatch.setattr(f"{MOD}.get_db", lambda: iter([MagicMock()]))
    monkeypatch.setattr(
        f"{MOD}.kill_switch_service.tools_halted", lambda db, account_id: False
    )
    monkeypatch.setattr(
        f"{MOD}.crud_tool_configuration.get_multi_by_account",
        lambda *a, **k: list(configs),
    )
    monkeypatch.setattr(
        mcp,
        "list_tools",
        AsyncMock(
            return_value=[Tool(name=t, description="d", parameters={}) for t in tools]
        ),
    )
    persist = MagicMock()
    monkeypatch.setattr(mcp, "_persist_tool_call_activity", persist)
    monkeypatch.setattr(storage, "_load_config", lambda account_id: None)
    audit_service = _audit_plugin(monkeypatch)
    return mcp, audit_service, persist


def _assert_declined(audit_service, persist, tool_name):
    audit_service.log_tool_call_async.assert_called_once()
    kwargs = audit_service.log_tool_call_async.call_args.kwargs
    assert kwargs["result"] == AUDIT_TOOL_CALL_DECLINED
    assert kwargs["tool_name"] == tool_name
    assert kwargs["correlation_id"]
    assert kwargs["correlation_id"] == persist.call_args.kwargs["correlation_id"]
    return kwargs


async def test_kill_switch_refusal_is_audited(monkeypatch, user_context):
    mcp, audit, persist = _refusal_setup(monkeypatch, user_context)
    monkeypatch.setattr(
        f"{MOD}.kill_switch_service.tools_halted", lambda db, account_id: True
    )
    result = await mcp.call_tool("safe_tool", {"owner": SECRET_DID})
    assert result.is_error
    kwargs = _assert_declined(audit, persist, "safe_tool")
    assert kwargs["error_code"] == "refused"
    assert kwargs["error_reason"]


async def test_kill_switch_check_failure_is_audited(monkeypatch, user_context):
    mcp, audit, persist = _refusal_setup(monkeypatch, user_context)

    def boom(db, account_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(f"{MOD}.kill_switch_service.tools_halted", boom)
    result = await mcp.call_tool("safe_tool", {})
    assert result.is_error
    _assert_declined(audit, persist, "safe_tool")


async def test_disabled_builtin_refusal_is_audited(monkeypatch, user_context):
    from preloop.api.endpoints.tools import BUILTIN_TOOLS

    name = BUILTIN_TOOLS[0]["name"]
    config = SimpleNamespace(
        tool_name=name,
        tool_source="builtin",
        is_enabled=False,
        justification_mode=None,
        managed_agent_id=None,
    )
    mcp, audit, persist = _refusal_setup(
        monkeypatch, user_context, configs=[config], tools=(name,)
    )
    result = await mcp.call_tool(name, {})
    assert result.is_error
    assert "disabled" in result.content[0].text
    _assert_declined(audit, persist, name)


async def test_missing_justification_refusal_is_audited(monkeypatch, user_context):
    config = SimpleNamespace(
        tool_name="safe_tool",
        tool_source="mcp",
        is_enabled=True,
        justification_mode="required",
        managed_agent_id=None,
    )
    mcp, audit, persist = _refusal_setup(monkeypatch, user_context, configs=[config])
    result = await mcp.call_tool("safe_tool", {"scope": "daily"})
    assert result.is_error
    assert "Justification required" in result.content[0].text
    _assert_declined(audit, persist, "safe_tool")


async def test_not_available_refusal_is_audited_for_api_key(monkeypatch, user_context):
    user_context.api_key_id = str(uuid4())
    user_context.api_key_name = "ci-key"
    mcp, audit, persist = _refusal_setup(monkeypatch, user_context, tools=())
    result = await mcp.call_tool("no_such_tool", {"owner": SECRET_DID})
    assert "is not available" in result.content[0].text
    kwargs = _assert_declined(audit, persist, "no_such_tool")
    assert kwargs["api_key_id"] == user_context.api_key_id
    assert kwargs["runtime_session_id"] is None
    assert kwargs["tool_args"] == {"owner": SECRET_DID}


async def test_sensitive_policy_load_failure_is_audited_without_values(
    monkeypatch, user_context
):
    mcp, audit, persist = _refusal_setup(monkeypatch, user_context)

    def fail(account_id):
        raise RuntimeError("policy store down")

    monkeypatch.setattr(f"{MOD}._load_sensitive_data_policy", fail)
    monkeypatch.setattr(storage, "_load_config", fail)
    result = await mcp.call_tool("safe_tool", {"owner": SECRET_DID})
    assert "could not be loaded" in result.content[0].text
    kwargs = _assert_declined(audit, persist, "safe_tool")
    assert kwargs["tool_args"] == {"arguments_withheld": True, "arg_keys": ["owner"]}
    assert SECRET_DID not in repr(kwargs)


async def test_internal_name_refusal_is_audited_with_correlation_id(
    monkeypatch, user_context
):
    mcp, audit, persist = _refusal_setup(monkeypatch, user_context)
    internal = f"account_{user_context.account_id.replace('-', '_')}_safe_tool"
    mcp._registered_proxied_tools.add(internal)
    result = await mcp.call_tool(internal, {})
    assert result.is_error
    _assert_declined(audit, persist, "safe_tool")


async def test_refusal_under_reference_only_stores_reference_record(
    monkeypatch, user_context, mocker
):
    accounts: dict = {}

    def get(db, id):  # noqa: A002 - mirrors crud signature
        account = MagicMock()
        account.meta_data = {}
        return accounts.setdefault(str(id), account)

    mocker.patch.object(reference.crud_account, "get", side_effect=get)
    mocker.patch(
        "preloop.models.db.session.get_session_factory",
        return_value=lambda: MagicMock(),
    )
    mocker.patch.object(reference, "flag_modified")
    reference.invalidate_salt_cache()
    config = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {
                    "id": "owner-data",
                    "scope": {"tools": ["no_such_tool"]},
                    "keep_fields": ["$.scope"],
                    "approver_view": "redacted",
                }
            ]
        }
    )
    mcp, audit, persist = _refusal_setup(monkeypatch, user_context, tools=())
    monkeypatch.setattr(storage, "_load_config", lambda account_id: config)
    await mcp.call_tool("no_such_tool", {"scope": "daily", "owner": SECRET_DID})
    kwargs = _assert_declined(audit, persist, "no_such_tool")
    record = kwargs["tool_args"]
    assert reference.is_reference_record(record)
    assert record["rule_id"] == "owner-data"
    assert record["kept"] == {"$.scope": "daily"}
    assert SECRET_DID not in repr(record)
    reference.invalidate_salt_cache()


# ---------------------------------------------------------------------------
# #1368: no unsalted argument hash on usage rows under reference-only
# ---------------------------------------------------------------------------


def _usage_metadata(monkeypatch, user_context, config):
    from preloop.models.crud import crud_runtime_session_activity

    captured = {}

    def log_tool_call(db, **kwargs):
        captured.update(kwargs["metadata"])
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(crud_runtime_session_activity, "log_tool_call", log_tool_call)
    monkeypatch.setattr(f"{MOD}.get_db", lambda: iter([MagicMock()]))
    user_context.runtime_session_id = str(uuid4())
    mcp = DynamicFastMCP("hash-test")
    try:
        mcp._persist_tool_call_activity(
            user_context,
            tool_name="read_scope",
            client_tool_name="read_scope",
            status="succeeded",
            summary="ok",
            arguments={"scope": "daily", "owner": SECRET_DID},
            correlation_id="c-1",
            storage_config=config,
        )
    except RuntimeError:
        pass
    return captured


async def test_usage_row_keeps_hash_without_reference_rule(monkeypatch, user_context):
    metadata = _usage_metadata(
        monkeypatch, user_context, SensitiveDataConfig.model_validate({})
    )
    assert metadata["arguments_hash"]
    assert metadata["arguments_summary"]


async def test_usage_row_drops_hash_under_reference_rule(monkeypatch, user_context):
    config = SensitiveDataConfig.model_validate(
        {
            "reference_only": [
                {"id": "r", "scope": {"tools": ["read_scope"]}, "keep_fields": []}
            ]
        }
    )
    metadata = _usage_metadata(monkeypatch, user_context, config)
    assert metadata["arguments_hash"] is None
    assert metadata["correlation_id"] == "c-1"
