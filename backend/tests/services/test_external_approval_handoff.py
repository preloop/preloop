"""External consent holds one call and retries it once without approver text."""

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.types import TextContent

from tests.services.test_proxied_tool_error_audit import _setup
from preloop.services.dynamic_mcp_server import UserContext
from uuid import uuid4

URL = "https://consent.example.com/decide?request=a%2Fb&state=x%2By"
REFERENCE = "request-example"
RESULT = {"external_reference": REFERENCE, "grant_reference": "grant-example"}
COMMENT = "Private approver text must never reach the agent"


@pytest.fixture
def user_context() -> UserContext:
    """Supply a caller with only the synthetic proxied tool enabled."""
    return UserContext(
        user_id=str(uuid4()),
        account_id=str(uuid4()),
        username="testuser",
        has_tracker=True,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["approved", "declined", "expired", "still_refused"]
)
async def test_scope_refusal_holds_call_and_retries_once(
    monkeypatch: pytest.MonkeyPatch, user_context: Any, outcome: str
) -> None:
    """A stub upstream changes its grant only after the external decision."""
    permitted = False
    calls = 0
    deliveries: list[dict[str, Any]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if not permitted:
            return httpx.Response(
                403,
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
                json={"error": "insufficient_scope"},
            )
        return httpx.Response(200, json={"value": "approved read"})

    def approver(request: httpx.Request) -> httpx.Response:
        deliveries.append(json.loads(request.content))
        return httpx.Response(
            200, json={"external_reference": REFERENCE, "external_url": URL}
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client,
        httpx.AsyncClient(transport=httpx.MockTransport(approver)) as approver_client,
    ):
        mcp, client, audit, server = _setup(monkeypatch, user_context)
        server.auth_config = {
            "on_insufficient_scope": "request_approval",
            "approval_workflow": "00000000-0000-4000-8000-000000000001",
        }

        async def call_tool(*args: Any, **kwargs: Any) -> list[TextContent]:
            response = await upstream_client.post("https://upstream.example.com/mcp")
            response.raise_for_status()
            return [TextContent(type="text", text=response.json()["value"])]

        async def approval(**kwargs: Any) -> tuple[bool, str | None]:
            nonlocal permitted
            if kwargs.get("workflow_id") != server.auth_config["approval_workflow"]:
                return True, None
            response = await approver_client.post(
                "https://consent.example.com/webhook", json={"type": "approval.created"}
            )
            assert response.json()["external_url"] == URL
            permitted = outcome == "approved"
            return (outcome in {"approved", "still_refused"}, None)

        client.call_tool = AsyncMock(side_effect=call_tool)
        monkeypatch.setattr(
            "preloop.services.approval_helper.require_approval", approval
        )
        result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert len(deliveries) == 1
    assert calls == (2 if outcome in {"approved", "still_refused"} else 1)
    assert result.is_error is (outcome != "approved")
    assert COMMENT not in str(result)
    if outcome == "approved":
        assert result.content[0].text == "approved read"
    else:
        assert "insufficient_scope" not in result.content[0].text
    rows = [call.kwargs for call in audit.log_tool_call_async.call_args_list]
    assert rows[0]["result"] == "upstream_error"
    assert len({row["correlation_id"] for row in rows}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["approved", "declined", "expired", "still_refused"]
)
async def test_external_handoff_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Any,
    test_user: Any,
    user_context: Any,
    outcome: str,
) -> None:
    """Exercise dispatch, approval wait, signed webhook, token decision and audit."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock

    from preloop.api.endpoints import public_approval
    from preloop.models import models
    from preloop.models.crud import crud_approval_workflow, crud_tool_configuration
    from preloop.models.schemas.tool_configuration import (
        ApprovalWorkflowCreate,
        ToolConfigurationCreate,
    )
    from preloop.services import approval_helper
    from preloop.services.approval_service import ApprovalService
    from preloop.services.audit.service import AuditService
    from preloop.services.event_webhooks import worker
    from preloop.services.dynamic_fastmcp import _correlation_id_var

    user_context.account_id = str(test_user.account_id)
    user_context.user_id = str(test_user.id)
    workflow = crud_approval_workflow.create(
        db_session,
        account_id=str(test_user.account_id),
        obj_in=ApprovalWorkflowCreate(
            name="External consent",
            account_id=str(test_user.account_id),
            approval_type="webhook",
            approver_user_ids=[str(test_user.id)],
            approval_config={
                "external_approval_link": True,
                "expose_comment_to_agent": False,
            },
            channel_configs={"webhook": {"url": "https://consent.example.com/webhook"}},
        ),
    )
    crud_tool_configuration.create(
        db_session,
        config_in=ToolConfigurationCreate(
            **{
                "account_id": str(test_user.account_id),
                "tool_name": "safe_tool",
                "tool_source": "mcp",
                "is_enabled": True,
            }
        ),
    )
    bridge = MagicMock()
    bridge.add.side_effect = db_session.add
    for name in ("execute", "scalar", "flush", "commit", "refresh", "rollback", "get"):
        setattr(bridge, name, AsyncMock(side_effect=getattr(db_session, name)))
    bridge.run_sync = AsyncMock(side_effect=lambda fn: fn(db_session))
    bridge.close = AsyncMock()

    @asynccontextmanager
    async def session() -> Any:
        yield bridge

    real_require = approval_helper.require_approval
    mcp, client, audit, server = _setup(monkeypatch, user_context)
    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    monkeypatch.setattr(public_approval, "get_async_db_session", session)
    monkeypatch.setattr(approval_helper, "require_approval", real_require)
    monkeypatch.setattr(ApprovalService, "_broadcast_approval_update", AsyncMock())
    monkeypatch.setattr(ApprovalService, "_broadcast_pending", AsyncMock())
    monkeypatch.setattr(ApprovalService, "_enqueue_approval_webhook", AsyncMock())
    monkeypatch.setattr(ApprovalService, "_release_parked_executions", AsyncMock())
    monkeypatch.setattr(
        "preloop.services.chat_worker.enqueue_approval_notifications", lambda *args: 0
    )
    email = AsyncMock()
    monkeypatch.setattr("preloop.utils.email.send_approval_request_email", email)
    monkeypatch.setattr(
        ApprovalService, "_resolve_bypass", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(ApprovalService, "_audit_notification_result", AsyncMock())
    monkeypatch.setattr(
        ApprovalService,
        "_partition_approvers_for_stagger",
        AsyncMock(
            return_value={
                "push_capable": [],
                "email_only": [test_user.id],
                "both_stagger": [],
                "both_immediate": [],
                "all_email": [test_user.id],
            }
        ),
    )
    server.id = "00000000-0000-4000-8000-000000000002"
    pool = MagicMock(get_client=AsyncMock(return_value=client))
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp.get_mcp_client_pool", lambda: pool
    )
    server.auth_type = "bearer"
    server.auth_config = {
        "token": "before",
        "on_insufficient_scope": "request_approval",
        "approval_workflow": str(workflow.id),
    }
    permitted = False
    call_count = 0
    delivery_count = 0
    captured_request = None
    lifecycle: list[dict[str, Any]] = []

    async def record_lifecycle(self: Any, **fields: Any) -> None:
        lifecycle.append(fields)
        AuditService().log_approval_lifecycle(db_session, **fields)

    monkeypatch.setattr(ApprovalService, "_enqueue_lifecycle", record_lifecycle)
    progress: list[str] = []

    async def gated(**kwargs: Any) -> tuple[bool, str]:
        ctx = MagicMock()
        ctx.report_progress = AsyncMock(
            side_effect=lambda **kw: progress.append(kw["message"])
        )
        ctx.session.send_progress_notification = AsyncMock()
        kwargs["ctx"] = ctx
        return await real_require(**kwargs)

    monkeypatch.setattr(approval_helper, "require_approval", gated)

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if not permitted:
            return httpx.Response(403, json={"error": "insufficient_scope"})
        assert server.auth_config["token"] == "after"
        return httpx.Response(200, json={"value": "approved read"})

    def approver(request: httpx.Request) -> httpx.Response:
        nonlocal delivery_count
        delivery_count += 1
        payload = json.loads(request.content)
        assert "/approve?token=" in payload["decision"]["approve_url"]
        return httpx.Response(
            200, json={"external_reference": REFERENCE, "external_url": URL}
        )

    real_post = ApprovalService.post_webhook_notification
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client,
        httpx.AsyncClient(transport=httpx.MockTransport(approver)) as approver_client,
    ):

        async def post(self: Any, request: Any, wf: Any) -> bool:
            nonlocal permitted, captured_request
            captured_request = request
            assert await real_post(self, request, wf)
            prepared = worker.claim_batch(db_session)
            assert len(prepared) == 1
            attempt = await worker.post_delivery(approver_client, prepared[0])
            assert attempt.success
            worker.apply_outcomes(db_session, [attempt])
            db_session.refresh(request)
            assert request.external_url == URL
            # A delivery retry or recovered claim can outlive the ordinary
            # 30-second notification guard. The durable worker must still send.
            request.requested_at = datetime.utcnow() - timedelta(seconds=300)
            db_session.commit()
            await worker.send_external_notifications()
            db_session.refresh(request)
            assert request.external_handoff_state == "ready"
            if outcome == "expired":
                request.expires_at = datetime.utcnow() - timedelta(seconds=1)
                request.status = "expired"
                db_session.commit()
            else:
                # The decision is a separate request with no originating MCP context.
                token = _correlation_id_var.set(None)
                try:
                    await public_approval.decide_approval_request_public(
                        request.id,
                        "decline" if outcome == "declined" else "approve",
                        token=request.approval_token,
                        body=public_approval.TokenDecisionBody(
                            comment=COMMENT, result=RESULT
                        ),
                        db_sync=db_session,
                    )
                finally:
                    _correlation_id_var.reset(token)
            server.auth_config = {**server.auth_config, "token": "after"}
            permitted = outcome == "approved"
            return True

        async def call_tool(*args: Any, **kwargs: Any) -> list[TextContent]:
            response = await upstream_client.post("https://upstream.example.com/mcp")
            response.raise_for_status()
            return [TextContent(type="text", text=response.json()["value"])]

        monkeypatch.setattr(ApprovalService, "post_webhook_notification", post)
        client.call_tool = AsyncMock(side_effect=call_tool)
        result = await mcp.call_tool("safe_tool", {"ok": "yes"})

    assert delivery_count == 1
    assert call_count == (2 if outcome in {"approved", "still_refused"} else 1)
    assert result.is_error is (outcome != "approved")
    assert COMMENT not in str(result) + str(progress)
    email.assert_awaited_once()
    assert email.call_args.kwargs["approval_url"] == URL
    assert email.call_args.kwargs["external_decision"] is True
    if outcome in {"approved", "still_refused"}:
        assert pool.get_client.call_args.kwargs["auth_config"]["token"] == "after"
    assert captured_request.external_reference == REFERENCE
    if outcome != "expired":
        assert (
            captured_request.external_result["grant_reference"]
            == RESULT["grant_reference"]
        )
        decision = lifecycle[-1]
        assert (
            decision["extra_details"]["external_result"]["grant_reference"]
            == RESULT["grant_reference"]
        )
        assert decision["correlation_id"] == lifecycle[0]["correlation_id"]
    assert (
        audit.log_tool_call_async.call_args_list[0].kwargs["correlation_id"]
        == lifecycle[0]["correlation_id"]
    )
    assert (
        db_session.query(models.ApprovalRequest)
        .filter_by(account_id=test_user.account_id)
        .count()
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_async_status_replays_once_and_withholds_comments(
    monkeypatch: pytest.MonkeyPatch,
    user_context: Any,
    refused: bool,
) -> None:
    """The actual polling callback claims, executes and caches one scope retry."""
    from contextlib import asynccontextmanager
    from datetime import datetime
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import UUID, uuid4

    from preloop.models import models
    from preloop.services.initialize_mcp import initialize_mcp_with_tools
    from preloop.services.mcp_client_pool import UpstreamToolContent

    upstream = UpstreamToolContent(
        [TextContent(type="text", text="approved read")], is_error=refused
    )
    proxy, client, _, server = _setup(monkeypatch, user_context, upstream=upstream)
    server.id = "00000000-0000-4000-8000-000000000002"
    server.auth_config = {
        "on_insufficient_scope": "request_approval",
        "approval_workflow": str(uuid4()),
        "token": "current",
    }
    status_server = initialize_mcp_with_tools()
    tool = await status_server.get_tool("get_approval_status")
    approval = models.ApprovalRequest(
        id=uuid4(),
        account_id=UUID(user_context.account_id),
        status="approved",
        tool_name="safe_tool",
        tool_args={"ok": "yes"},
        responses=[],
        approver_comment=COMMENT,
        external_url=URL,
        external_reference=REFERENCE,
        rule_context={
            "source": "upstream_scope_refusal",
            "server_id": server.id,
            "upstream_name": "safe_tool",
        },
    )
    approval.approval_workflow = models.ApprovalWorkflow(
        approval_config={"external_approval_link": True}
    )
    event = SimpleNamespace(
        timestamp=datetime.utcnow(),
        event_type="vote_received",
        detail="Approval vote received",
        comment=COMMENT,
    )
    approval_result = MagicMock()
    approval_result.scalar_one_or_none.return_value = approval
    events_result = MagicMock()
    events_result.scalars.return_value = [event]
    db = AsyncMock()
    db.run_sync = AsyncMock(side_effect=lambda fn: fn(MagicMock()))
    db.execute.side_effect = [
        approval_result,
        events_result,
        approval_result,
        approval_result,
        events_result,
    ]

    @asynccontextmanager
    async def session() -> Any:
        yield db

    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: user_context,
    )
    status_server._registered_proxied_tools = proxy._registered_proxied_tools
    replay = AsyncMock(side_effect=proxy.call_registered_tool_without_policy)
    monkeypatch.setattr(status_server, "call_registered_tool_without_policy", replay)
    monkeypatch.setattr(
        "preloop.services.approval_service._log_approval_tool_executed_async",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp._approval_correlation_id",
        AsyncMock(return_value="correlation-example"),
    )
    introspection = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp._evaluate_snapshot_grant", introspection
    )

    first = json.loads(await tool.fn(request_id=str(approval.id)))
    second = json.loads(await tool.fn(request_id=str(approval.id)))
    assert COMMENT not in json.dumps(first) + json.dumps(second)
    assert first["tool_result"] == second["tool_result"]
    assert first["external_reference"] == REFERENCE
    client.call_tool.assert_awaited_once()
    replay.assert_awaited_once()
    assert any(
        call.kwargs.get("force") is True for call in introspection.call_args_list
    )
    if refused:
        assert "approved but retry refused" in json.dumps(first["tool_result"])
    else:
        assert "approved read" in json.dumps(first["tool_result"])
