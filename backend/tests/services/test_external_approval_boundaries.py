"""Untrusted webhook answers and decisions stay bounded and private."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from preloop.models.schemas.external_approval import ExternalApprovalResult
from preloop.services.external_approval import (
    allowed_hosts,
    hide_agent_comment,
    parse_handoff,
    scope_refusal,
)
from preloop.services.event_webhooks.worker import PreparedDelivery, post_delivery
from preloop.services.push_notifications.notification_payloads import (
    NotificationPayloadBuilder,
)

URL = "https://consent.example.com/path?request=a%2Fb&state=x%2By"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["request_too_old", "notifications_muted"])
async def test_notification_worker_preserves_unexpected_skips(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Unexpected skips remain retryable; an intentional mute stays respected."""
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock
    from uuid import uuid4

    from preloop.models.crud import external_approval as handoff_crud
    from preloop.services.approval_service import ApprovalService
    from preloop.services.event_webhooks.worker import send_external_notifications

    request = SimpleNamespace(
        id=uuid4(), status="pending", approval_workflow=SimpleNamespace()
    )
    db = AsyncMock()
    db.run_sync.side_effect = lambda fn: fn(None)

    @asynccontextmanager
    async def session() -> Any:
        yield db

    monkeypatch.setattr("preloop.models.db.session.get_async_db_session", session)
    monkeypatch.setattr(handoff_crud, "pending_notifications", lambda db: [request.id])
    monkeypatch.setattr(
        handoff_crud, "notification_request", lambda db, request_id: request
    )
    finish = MagicMock()
    monkeypatch.setattr(handoff_crud, "notifications_finished", finish)
    notify = AsyncMock(return_value={"skipped": True, "reason": reason})
    monkeypatch.setattr(ApprovalService, "send_notifications", notify)

    await send_external_notifications()
    if reason == "notifications_muted":
        finish.assert_called_once()
    else:
        finish.assert_not_called()
        notify.return_value = {"email": {"success": True, "sent": 1}}
        await send_external_notifications()
        finish.assert_called_once()
        assert notify.await_count == 2


def test_handoff_carries_url_byte_for_byte() -> None:
    """Validation never normalizes percent encoding or query order."""
    data = {"external_reference": "request-example", "external_url": URL}
    assert parse_handoff(json.dumps(data).encode(), ("consent.example.com",)) == data
    assert allowed_hosts(
        SimpleNamespace(approval_config={}), "https://consent.example.com/hook"
    ) == ("consent.example.com",)


@pytest.mark.parametrize(
    "url",
    [
        "http://consent.example.com/",
        "https://evil.example.com/",
        "https://consent.example.com.evil.example.com/",
        "https://consent.example.com@evil.example.com/",
        "https://user@consent.example.com/",
        "https://consent.example.com:8443/",
        "https://consent.example.com./",
        "https://consent.example.com\\@evil.example.com/",
        "https://consent.example.com/\nnext",
        "//consent.example.com/path",
        'https://consent.example.com/"onclick=evil',
        "https://cоnsent.example.com/",
    ],
)
def test_unsafe_external_url_is_rejected(url: str) -> None:
    """Exact-host HTTPS checks resist userinfo, suffix and parser confusion."""
    with pytest.raises(ValueError):
        parse_handoff(
            json.dumps(
                {"external_reference": "request-example", "external_url": url}
            ).encode(),
            ("consent.example.com",),
        )


@pytest.mark.parametrize(
    "result",
    [
        {"fields": {"nested": {"value": "x"}}},
        {"fields": {"number": 2}},
        {"fields": {"flag": True}},
        {"fields": {"null": None}},
        {"unexpected": "value"},
        {"external_reference": "x" * 513},
        {"fields": {"x": "x" * 2049}},
        {"fields": {str(i): "x" for i in range(33)}},
        {"fields": {str(i): "x" * 2000 for i in range(5)}},
    ],
)
def test_result_is_flat_string_only_and_bounded(result: dict[str, Any]) -> None:
    """Nested, untyped and oversized evidence cannot enter the decision store."""
    with pytest.raises(ValidationError):
        ExternalApprovalResult.model_validate(result)


@pytest.mark.parametrize(
    "status,settings,url,hidden",
    [
        ("approved", {}, None, False),
        ("declined", {}, URL, False),
        ("approved", {}, URL, True),
        ("approved", {"external_approval_link": True}, None, True),
        ("declined", {"expose_comment_to_agent": False}, URL, True),
    ],
)
def test_comment_visibility(
    status: str, settings: dict, url: str | None, hidden: bool
) -> None:
    """Default decline comments remain compatible; external approvals stay private."""
    request = SimpleNamespace(
        status=status,
        external_url=url,
        approval_workflow=SimpleNamespace(approval_config=settings),
    )
    assert bool(hide_agent_comment(request)) is hidden


def test_push_payload_has_external_link_and_no_local_actions() -> None:
    """A separate category prevents existing mobile approve/decline actions."""
    payload = NotificationPayloadBuilder.new_approval_request(
        "request-example",
        "read",
        external_url=URL,
    )
    assert payload["external_url"] == payload["data"]["external_url"] == URL
    assert payload["aps"]["category"] == "EXTERNAL_APPROVAL"
    assert "external service" in payload["aps"]["alert"]["body"]
    assert "/approval/" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {
            "external_reference": "request-example",
            "external_url": "https://evil.example.com/",
        },
        {"external_reference": "request-example"},
        {"external_url": URL},
    ],
)
async def test_webhook_invalid_handoff_is_a_recorded_failure(body: dict) -> None:
    """A 2xx with an unsafe URL must not count as a successful delivery."""
    prepared = PreparedDelivery(
        "delivery",
        "endpoint",
        "https://consent.example.com/hook",
        "test-secret",
        "event",
        "approval.created",
        1,
        b"{}",
        external_hosts=("consent.example.com",),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        outcome = await post_delivery(client, prepared)
    assert not outcome.success
    assert outcome.permanent_failure
    assert "rejected" in outcome.error
    assert outcome.handoff is None


@pytest.mark.asyncio
async def test_webhook_body_is_bounded_before_parsing() -> None:
    """An external response cannot cause unbounded body retention."""
    prepared = PreparedDelivery(
        "delivery",
        "endpoint",
        "https://consent.example.com/hook",
        "test-secret",
        "event",
        "approval.created",
        1,
        b"{}",
        external_hosts=("consent.example.com",),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 16385)
        )
    ) as client:
        outcome = await post_delivery(client, prepared)
    assert outcome.permanent_failure
    assert "16 KiB" in outcome.error


@pytest.mark.parametrize(
    "status,body,header,expected",
    [
        (403, {"error": "insufficient_scope"}, "", True),
        (403, {}, 'Bearer error="insufficient_scope"', True),
        (401, {"error": "insufficient_scope"}, "", False),
        (500, {"error": "insufficient_scope"}, "", False),
        (403, {"error": "access_denied"}, "", False),
    ],
)
def test_only_scope_403_triggers(
    status: int, body: dict, header: str, expected: bool
) -> None:
    """Other auth and transport errors never start approvals."""
    request = httpx.Request("POST", "https://upstream.example.com/mcp")
    response = httpx.Response(
        status, request=request, json=body, headers={"WWW-Authenticate": header}
    )
    exc = httpx.HTTPStatusError("refused", request=request, response=response)
    assert scope_refusal(exc) is expected


@pytest.mark.asyncio
async def test_structured_result_runs_the_tool_argument_redactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored and audited copies receive the same scoped redaction."""
    from preloop.services.approval_service import ApprovalService

    request = SimpleNamespace(
        account_id="00000000-0000-4000-8000-000000000001",
        tool_name="read",
        server_name="upstream",
        managed_agent_id=None,
        external_reference="request-example",
    )
    seen = []

    def redact(account_id: str, value: dict, **kwargs: Any) -> dict:
        seen.append((value, kwargs["scope"]))
        return {**value, "fields": {"private": "[REDACTED]"}}

    monkeypatch.setattr(
        "preloop.services.sensitive_data.storage.apply_storage_redaction", redact
    )
    stored = await ApprovalService(AsyncMock(), "")._external_decision_fields(
        request,
        {
            "external_reference": "request-example",
            "grant_reference": "grant-example",
            "fields": {"private": "sensitive", "token": "secret"},
        },
    )
    assert "secret" not in str(seen[0][0])
    assert stored["external_result"]["fields"]["private"] == "[REDACTED]"
    assert seen[0][1].target == "tool.args"
    assert seen[0][1].server_name == "upstream"


def test_parked_resume_never_carries_external_approval_comment() -> None:
    """A parked flow is another agent output boundary, not an audit reader."""
    from preloop.services.approval_park import answer_from_request

    request = SimpleNamespace(
        id="request-example",
        status="approved",
        external_url=URL,
        approval_workflow=SimpleNamespace(approval_config={}),
        tool_name="read",
        tool_args={},
        responses=[],
        approver_comment="private approver text",
    )
    assert answer_from_request(request)["answer"] == ""


@pytest.mark.asyncio
async def test_mcp_transport_retains_only_scope_refusal_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real client must not discard the 403 trigger while sanitizing errors."""
    from tests.services.test_mcp_client_pool import mock_mcp_session
    from preloop.services.mcp_client_pool import MCPClient, MCPAuthenticationError

    streams, session = mock_mcp_session()
    request = httpx.Request("POST", "https://upstream.example.com/mcp")
    response = httpx.Response(
        403,
        request=request,
        json={"error": "insufficient_scope", "private": "do not retain"},
    )
    session.call_tool.side_effect = httpx.HTTPStatusError(
        "refused", request=request, response=response
    )
    monkeypatch.setattr(
        "preloop.services.mcp_client_pool.streamable_http_client",
        lambda *a, **kw: streams,
    )
    monkeypatch.setattr(
        "preloop.services.mcp_client_pool.ClientSession", lambda *a, **kw: session
    )
    client = MCPClient(url="https://upstream.example.com/mcp")
    client._connected = True
    with pytest.raises(MCPAuthenticationError) as raised:
        await client.call_tool("read", {})
    assert scope_refusal(raised.value)
    assert not hasattr(raised.value, "response")
    assert "do not retain" not in str(raised.value)
