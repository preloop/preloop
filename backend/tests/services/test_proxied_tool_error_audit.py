"""Upstream tool errors are forwarded and audited with their real outcome.

Regression: an upstream ``isError`` result lost ``isError`` and
``structuredContent`` through the proxy, and the ``tool_call`` audit row was
``executed`` for upstream errors, raised HTTP 401s and approval declines.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastmcp.tools import Tool
from fastmcp.tools.tool import ToolResult
from mcp import types

from preloop.services.dynamic_fastmcp import DynamicFastMCP
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.mcp_client_pool import MCPClient, UpstreamToolContent

pytestmark = pytest.mark.asyncio

SERVER_ID = "server-123"
DENIED = {"error": {"code": "insufficient_scope", "message": "scope not granted"}}


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


def _setup(monkeypatch, user_context, *, upstream=None, raises=None, approve=True):
    """Register ``safe_tool`` as a proxied tool behind a fake upstream."""
    mcp = DynamicFastMCP("test-mcp")
    mcp.set_user_context_provider(lambda: user_context)
    client = MagicMock()
    client.call_tool = AsyncMock(return_value=upstream, side_effect=raises)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=MagicMock())
    session.__aexit__ = AsyncMock(return_value=None)
    audit_service = MagicMock()
    plugin_manager = MagicMock()
    plugin_manager.get_service.side_effect = lambda key: (
        audit_service if key == "audit_service" else None
    )
    server_row = MagicMock(
        url="http://upstream.example.test",
        auth_type="none",
        auth_config={},
        transport="http",
        last_error=None,
    )
    server_row.name = "upstream"
    mod = "preloop.services.dynamic_fastmcp"
    monkeypatch.setattr(f"{mod}.get_db", lambda: iter([MagicMock()]))
    monkeypatch.setattr(
        f"{mod}.kill_switch_service.tools_halted", lambda db, account_id: False
    )
    monkeypatch.setattr(
        f"{mod}.crud_tool_configuration.get_multi_by_account", lambda *a, **k: []
    )
    monkeypatch.setattr(
        f"{mod}.crud_mcp_server.get", MagicMock(return_value=server_row)
    )
    monkeypatch.setattr(
        f"{mod}._resolve_proxied_tool_server", MagicMock(return_value=server_row)
    )
    monkeypatch.setattr(
        f"{mod}.get_mcp_client_pool",
        lambda: MagicMock(get_client=AsyncMock(return_value=client)),
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_async_db_session", lambda: session
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async",
        AsyncMock(return_value=("allow", None, None)),
    )
    monkeypatch.setattr(
        "preloop.services.approval_helper.require_approval",
        AsyncMock(return_value=(True, None) if approve else (False, "Declined")),
    )
    monkeypatch.setattr(
        "preloop.plugins.base.get_plugin_manager", lambda: plugin_manager
    )
    monkeypatch.setattr(
        mcp,
        "list_tools",
        AsyncMock(
            return_value=[Tool(name="safe_tool", description="d", parameters={})]
        ),
    )
    monkeypatch.setattr(mcp, "_halt_dispatch_denial", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp, "_persist_tool_call_activity", MagicMock())

    wrapper = mcp._create_proxied_tool_wrapper(
        tool_name="safe_tool",
        account_id=user_context.account_id,
        description="Safe tool",
        input_schema={"properties": {"ok": {"type": "string"}}},
    )
    internal = f"account_{user_context.account_id.replace('-', '_')}_safe_tool"
    mcp.tool()(wrapper)
    mcp._registered_proxied_tools.add(internal)
    mcp._proxied_tool_servers["safe_tool"] = SERVER_ID
    return mcp, client, audit_service, server_row


def _audit_kwargs(audit_service):
    audit_service.log_tool_call_async.assert_called_once()
    return audit_service.log_tool_call_async.call_args.kwargs


async def test_upstream_is_error_and_structured_content_pass_through(
    monkeypatch, user_context
):
    upstream = UpstreamToolContent(
        [types.TextContent(type="text", text="403 insufficient_scope")],
        is_error=True,
        structured_content=DENIED,
    )
    mcp, _, audit_service, _ = _setup(monkeypatch, user_context, upstream=upstream)

    result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.structured_content == DENIED
    assert "insufficient_scope" in result.content[0].text
    kwargs = _audit_kwargs(audit_service)
    assert kwargs["result"] == "upstream_error"


async def test_structured_content_success_passes_through(monkeypatch, user_context):
    upstream = UpstreamToolContent(
        [types.TextContent(type="text", text="ok")],
        structured_content={"value": 1},
    )
    mcp, _, audit_service, _ = _setup(monkeypatch, user_context, upstream=upstream)

    result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert not result.is_error
    assert result.structured_content == {"value": 1}
    assert _audit_kwargs(audit_service)["result"] == "executed"


async def test_plain_success_still_executed(monkeypatch, user_context):
    upstream = [types.TextContent(type="text", text="ok")]
    mcp, _, audit_service, _ = _setup(monkeypatch, user_context, upstream=upstream)

    result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert not result.is_error
    assert result.content[0].text == "ok"
    assert _audit_kwargs(audit_service)["result"] == "executed"


async def test_upstream_http_401_is_upstream_error_and_sets_last_error(
    monkeypatch, user_context
):
    request = httpx.Request("POST", "http://upstream.example.test/mcp")
    response = httpx.Response(401, request=request)
    exc = httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
    mcp, _, audit_service, server_row = _setup(monkeypatch, user_context, raises=exc)

    result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert result.is_error is True
    assert _audit_kwargs(audit_service)["result"] == "upstream_error"
    assert server_row.last_error is not None
    assert "http_401" in server_row.last_error


async def test_transport_failure_is_failed_and_sets_last_error(
    monkeypatch, user_context
):
    mcp, _, audit_service, server_row = _setup(
        monkeypatch, user_context, raises=ConnectionError("connection refused")
    )

    result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert result.is_error is True
    assert _audit_kwargs(audit_service)["result"] == "failed"
    assert "connection refused" in server_row.last_error


async def test_declined_at_approval_is_declined_and_not_forwarded(
    monkeypatch, user_context
):
    mcp, client, audit_service, _ = _setup(
        monkeypatch, user_context, upstream=[], approve=False
    )

    result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert result.is_error is True
    client.call_tool.assert_not_called()
    assert _audit_kwargs(audit_service)["result"] == "declined"


async def test_error_code_and_reason_reach_audit_services_that_accept_them(
    monkeypatch, user_context
):
    upstream = UpstreamToolContent(
        [types.TextContent(type="text", text="denied")],
        is_error=True,
        structured_content=DENIED,
    )
    mcp, _, audit_service, _ = _setup(monkeypatch, user_context, upstream=upstream)
    calls = []

    def log_tool_call_async(*, error_code=None, error_reason=None, **kwargs):
        calls.append({"error_code": error_code, "error_reason": error_reason, **kwargs})

    audit_service.log_tool_call_async = log_tool_call_async

    await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert calls[0]["result"] == "upstream_error"
    assert calls[0]["error_code"] == "insufficient_scope"
    assert calls[0]["error_reason"] == "scope not granted"


async def test_error_kwargs_not_sent_to_older_audit_services(monkeypatch, user_context):
    """An audit service without the new parameters still gets its row."""
    upstream = UpstreamToolContent(
        [types.TextContent(type="text", text="denied")], is_error=True
    )
    mcp, _, audit_service, _ = _setup(monkeypatch, user_context, upstream=upstream)
    calls = []

    # Signature without error_code/error_reason and without **kwargs.
    def strict(
        db_factory,
        account_id,
        tool_name,
        tool_args,
        result=None,
        duration_ms=None,
        policy_decision=None,
        rule_matched=None,
        user_id=None,
        execution_id=None,
        correlation_id=None,
        runtime_session_id=None,
        runtime_principal_type=None,
        runtime_principal_id=None,
        runtime_principal_name=None,
        api_key_id=None,
        api_key_name=None,
    ):
        calls.append(result)

    audit_service.log_tool_call_async = strict

    await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert calls == ["upstream_error"]


async def test_client_call_tool_keeps_is_error_and_structured_content():
    client = MCPClient(url="http://localhost:8001/mcp")
    client._connected = True
    upstream = types.CallToolResult(
        content=[types.TextContent(type="text", text="denied")],
        structuredContent=DENIED,
        isError=True,
    )
    streams = AsyncMock()
    streams.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
    streams.__aexit__ = AsyncMock(return_value=None)
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.call_tool = AsyncMock(return_value=upstream)
    with (
        patch(
            "preloop.services.mcp_client_pool.streamablehttp_client",
            return_value=streams,
        ),
        patch("preloop.services.mcp_client_pool.ClientSession", return_value=session),
    ):
        result = await client.call_tool("t", {})

    assert [item.text for item in result] == ["denied"]
    assert result.is_error is True
    assert result.structured_content == DENIED


async def test_text_only_upstream_error_reason_comes_from_content(
    monkeypatch, user_context
):
    """Without structuredContent the reason is taken from the text blocks."""
    upstream = UpstreamToolContent(
        [types.TextContent(type="text", text="token expired for this scope")],
        is_error=True,
    )
    mcp, _, audit_service, _ = _setup(monkeypatch, user_context, upstream=upstream)
    calls = []
    audit_service.log_tool_call_async = lambda **kwargs: calls.append(kwargs)

    await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert calls[0]["result"] == "upstream_error"
    assert calls[0]["error_code"] == "tool_error"
    assert calls[0]["error_reason"] == "token expired for this scope"


@pytest.mark.parametrize("payload_status", ["pending_approval", "parked_for_human"])
async def test_pending_approval_is_not_audited_as_declined(
    monkeypatch, user_context, payload_status
):
    """An approval that is still open is pending, not declined."""
    mcp, client, audit_service, _ = _setup(monkeypatch, user_context, upstream=[])
    monkeypatch.setattr(
        "preloop.services.approval_helper.require_approval",
        AsyncMock(
            return_value=(
                False,
                json.dumps({"status": payload_status, "request_id": "r1"}),
            )
        ),
    )

    await mcp.call_tool("safe_tool", {"ok": "yes"})

    client.call_tool.assert_not_called()
    assert _audit_kwargs(audit_service)["result"] == "pending_approval"


async def test_post_approval_replay_reports_upstream_error(monkeypatch, user_context):
    """The async-poll replay path records upstream_error, not executed."""
    from preloop.services.dynamic_fastmcp import post_approval_exec_outcome

    upstream = UpstreamToolContent(
        [types.TextContent(type="text", text="denied")],
        is_error=True,
        structured_content=DENIED,
    )
    mcp, _, _, _ = _setup(monkeypatch, user_context, upstream=upstream)
    internal = f"account_{user_context.account_id.replace('-', '_')}_safe_tool"

    tool_result = await mcp.call_registered_tool_without_policy(
        internal, {"ok": "yes"}, account_id=user_context.account_id
    )
    status, error = post_approval_exec_outcome(tool_result)

    assert status == "upstream_error"
    assert error == "insufficient_scope: scope not granted"
    # Stamps are cleared, so a later success is not misread.
    ok = ToolResult(content=[types.TextContent(type="text", text="ok")])
    assert post_approval_exec_outcome(ok) == ("executed", None)


async def test_dropped_audit_fields_warn_once_without_logging_values(
    monkeypatch, caplog
):
    from preloop.services import dynamic_fastmcp as module

    monkeypatch.setattr(module, "_DROPPED_AUDIT_ERROR_FIELDS", set(), raising=False)
    monkeypatch.setattr(module.logger, "propagate", False)
    monkeypatch.setattr(module.logger, "handlers", [caplog.handler])
    service = MagicMock()
    service.log_tool_call_async = lambda: None
    for _ in range(2):
        assert (
            module._audit_error_kwargs(service, "private-code", "private-reason") == {}
        )
    messages = [record.message for record in caplog.records]
    assert len(messages) == 2
    assert any("error_code" in message for message in messages)
    assert any("error_reason" in message for message in messages)
    assert all("private-" not in message for message in messages)


async def test_uninspectable_audit_service_warns_once(monkeypatch, caplog):
    from preloop.services import dynamic_fastmcp as module

    monkeypatch.setattr(module, "_DROPPED_AUDIT_ERROR_FIELDS", set(), raising=False)
    monkeypatch.setattr(module.logger, "propagate", False)
    monkeypatch.setattr(module.logger, "handlers", [caplog.handler])
    with patch.object(module.inspect, "signature", side_effect=ValueError):
        for _ in range(2):
            assert module._audit_error_kwargs(MagicMock(), None, "private-reason") == {}
    assert len(caplog.records) == 1
    assert "error_reason" in caplog.records[0].message
    assert "private-reason" not in caplog.records[0].message
