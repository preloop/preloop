"""Approvals the terminal can see, and that end when the hook ends.

Covers the hook-deadline expiry endpoint, the rule that an approval policy
resolves on its own is never broadcast as pending, and UTC offsets on the
approval timestamps the console parses.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

from preloop.models import models
from preloop.models.schemas.approval_request import ApprovalRequestResponse
from preloop.services.approval_service import ApprovalService

TOKEN_URL = "/api/v1/auth/runtime-sessions/token"


@pytest.fixture(autouse=True)
def permission_identity_session(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint sync sessions share the test's rollback transaction."""
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(
        "preloop.api.endpoints.agent_permission.get_session_factory", lambda: factory
    )


def _runtime_token(client: Any) -> str:
    response = client.post(
        TOKEN_URL,
        json={
            "session_source_type": "opencode",
            "session_source_id": "opencode-example",
            "session_reference": "/home/dev/.config/opencode/opencode.json",
            "runtime_principal_name": "Example OpenCode",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _pending_row(db_session: Session, account_id: Any, agent_id: Any) -> Any:
    workflow = models.ApprovalWorkflow(
        account_id=account_id,
        name=f"expire-test-{uuid.uuid4().hex[:8]}",
        approval_type="manual",
        channel="email",
    )
    db_session.add(workflow)
    db_session.flush()
    config = models.ToolConfiguration(
        account_id=account_id,
        tool_name="Bash",
        tool_source="agent",
        approval_workflow_id=workflow.id,
        is_enabled=True,
        custom_config={},
    )
    db_session.add(config)
    db_session.flush()
    row = models.ApprovalRequest(
        account_id=account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="Bash",
        tool_args={"command": "ls"},
        managed_agent_id=agent_id,
        status="pending",
        requested_at=datetime.utcnow(),
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_expire_by_hook_marks_expired_and_broadcasts(client: Any, db_session: Session):
    """The hook's deadline expires its own approval and every surface hears it."""
    from preloop.api.auth.jwt import _managed_agent_for_api_key
    from preloop.models.crud import crud_api_key

    token = _runtime_token(client)
    key = crud_api_key.get_by_key(db_session, key=token)
    agent = _managed_agent_for_api_key(db_session, key)
    row = _pending_row(db_session, key.account_id, agent.id)

    expired = MagicMock(
        id=row.id,
        account_id=row.account_id,
        status="expired",
        approver_comment="Expired: the agent's permission hook stopped waiting within 9 min",
        tool_name="Bash",
        execution_id=None,
    )
    broadcast = AsyncMock()
    record = AsyncMock()
    update = AsyncMock(return_value=expired)

    @asynccontextmanager
    async def fake_db() -> AsyncIterator[Any]:
        yield MagicMock()

    with (
        patch("preloop.models.db.session.get_async_db_session", fake_db),
        patch.object(
            ApprovalService,
            "get_approval_request_for_update",
            AsyncMock(
                return_value=MagicMock(
                    id=row.id, account_id=row.account_id, status="pending"
                )
            ),
        ),
        patch.object(ApprovalService, "_record_event", record),
        patch.object(ApprovalService, "update_approval_request", update),
        patch.object(ApprovalService, "_broadcast_approval_update", broadcast),
    ):
        response = client.post(
            f"/api/v1/approvals/{row.id}/expire",
            json={"reason": "hook_deadline", "waited_seconds": 540},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "expired"
    assert update.await_args.args[1].status == "expired"
    assert record.await_args.kwargs["event_type"] == "expired"
    assert "9 min" in record.await_args.kwargs["detail"]
    broadcast.assert_awaited_once()
    assert broadcast.await_args.args[1] == "expired"
    assert broadcast.await_args.args[2] == {"expired_reason": "hook_deadline"}


def test_expire_by_hook_refuses_another_agents_approval(
    client: Any, db_session: Session
):
    """A hook credential cannot expire an approval it did not raise."""
    from preloop.models.crud import crud_api_key

    token = _runtime_token(client)
    key = crud_api_key.get_by_key(db_session, key=token)
    row = _pending_row(db_session, key.account_id, None)

    response = client.post(
        f"/api/v1/approvals/{row.id}/expire",
        json={"reason": "hook_deadline"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    unknown = client.post(
        f"/api/v1/approvals/{uuid.uuid4()}/expire",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert unknown.status_code == 404
    assert client.post(f"/api/v1/approvals/{row.id}/expire", json={}).status_code == 401


def _service_with_created_row(status_after: str = "pending") -> tuple:
    service = ApprovalService(MagicMock(), "https://preloop.example")
    row = MagicMock(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        tool_args={"command": "ls"},
        tool_name="Bash",
        execution_id=None,
        managed_agent_id=None,
        status="pending",
    )
    create = AsyncMock(return_value=row)
    events: list = []

    async def broadcast(
        request: Any, event_type: str, *args: Any, **kwargs: Any
    ) -> None:
        events.append((event_type, kwargs.get("webhook", True)))

    return service, row, create, events, broadcast


@pytest.mark.asyncio
async def test_policy_bypass_escalation_never_broadcast_pending():
    """Standing bypasses resolve before any live surface sees a pending card."""
    service, row, create, events, broadcast = _service_with_created_row()
    workflow = MagicMock(id=uuid.uuid4(), approval_config={}, timeout_seconds=300)
    approved = MagicMock(
        id=row.id,
        account_id=row.account_id,
        tool_name="Bash",
        execution_id=None,
        managed_agent_id=None,
        status="approved",
    )
    with (
        patch.object(ApprovalService, "create_approval_request", create),
        patch.object(
            ApprovalService,
            "update_approval_request",
            AsyncMock(side_effect=[row, approved]),
        ),
        patch.object(ApprovalService, "_require_consent_owner", AsyncMock()),
        patch.object(ApprovalService, "_record_event", AsyncMock()),
        patch.object(
            ApprovalService, "_broadcast_approval_update", side_effect=broadcast
        ),
        patch.object(ApprovalService, "send_notifications", AsyncMock()) as notify,
        patch(
            "preloop.services.approval_summary.generate_approval_summary",
            AsyncMock(return_value=None),
        ),
        patch("preloop.models.db.session.get_session_factory", lambda: MagicMock),
    ):
        result = await service.create_and_notify(
            account_id=str(row.account_id),
            tool_configuration_id=uuid.uuid4(),
            approval_workflow=workflow,
            tool_name="Bash",
            tool_args={"command": "ls"},
            standing_bypass_reason=models.AutoApprovedReason.NATIVE_TOOL_APPROVALS_OFF,
        )

    assert result is approved
    assert create.await_args.kwargs["broadcast_created"] is False
    assert [event for event, _ in events] == ["approved"]
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_approval_broadcasts_pending_once_without_second_webhook():
    """A request that waits for a person is broadcast as created exactly once."""
    service, row, create, events, broadcast = _service_with_created_row()
    workflow = MagicMock(
        id=uuid.uuid4(), approval_config={}, timeout_seconds=300, approval_mode="manual"
    )
    with (
        patch.object(ApprovalService, "create_approval_request", create),
        patch.object(
            ApprovalService, "update_approval_request", AsyncMock(return_value=row)
        ),
        patch.object(ApprovalService, "_resolve_bypass", AsyncMock(return_value=None)),
        patch.object(
            ApprovalService, "_broadcast_approval_update", side_effect=broadcast
        ),
        patch.object(ApprovalService, "send_notifications", AsyncMock()) as notify,
        patch(
            "preloop.services.approval_summary.generate_approval_summary",
            AsyncMock(return_value=None),
        ),
        patch("preloop.models.db.session.get_session_factory", lambda: MagicMock),
    ):
        await service.create_and_notify(
            account_id=str(row.account_id),
            tool_configuration_id=uuid.uuid4(),
            approval_workflow=workflow,
            tool_name="Bash",
            tool_args={"command": "ls"},
        )

    assert events == [("created", False)]
    notify.assert_awaited_once()


def test_approval_created_at_serialised_with_utc_offset():
    """Naive stored UTC leaves the API with an explicit offset."""
    row = models.ApprovalRequest(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        tool_configuration_id=uuid.uuid4(),
        approval_workflow_id=uuid.uuid4(),
        tool_name="Bash",
        tool_args={},
        status="pending",
        requested_at=datetime(2026, 10, 21, 19, 59, 51),
        expires_at=datetime(2026, 10, 21, 20, 9, 51),
        resolved_at=None,
        decided_by_ai=False,
    )
    body = ApprovalRequestResponse.model_validate(row).model_dump(mode="json")
    assert body["requested_at"] == "2026-10-21T19:59:51+00:00"
    assert body["expires_at"] == "2026-10-21T20:09:51+00:00"
    assert body["resolved_at"] is None
