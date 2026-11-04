"""Explicit notification recipients leave the stored workflow untouched."""

import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from preloop.models import models
from preloop.services import approval_service as service_module
from preloop.services.approval_service import ApprovalService


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, [], [uuid.UUID(int=2)]])
async def test_recipient_override_reaches_every_per_user_channel(
    monkeypatch: Any, override: list[uuid.UUID] | None
) -> None:
    from preloop.services import chat_worker

    requester = uuid.UUID(int=1)
    workflow = models.ApprovalWorkflow(approver_user_ids=[requester])
    request = models.ApprovalRequest(
        id=uuid.uuid4(), account_id=uuid.uuid4(), requested_at=datetime.utcnow()
    )
    service = ApprovalService(AsyncMock(), "https://app.example.com")
    expected = [requester] if override is None else override
    resolve = AsyncMock(return_value=[requester])
    mute = AsyncMock(return_value=None)
    push = AsyncMock(return_value={"success": True})
    email = AsyncMock(return_value={"success": True})
    chat = Mock(return_value=1)
    monkeypatch.setattr(service, "_get_all_approver_user_ids", resolve)
    monkeypatch.setattr(service, "_resolve_bypass", mute)
    monkeypatch.setattr(service, "_send_push_notification", push)
    monkeypatch.setattr(service, "_send_email_notification", email)
    monkeypatch.setattr(service, "_audit_notification_result", AsyncMock())
    monkeypatch.setattr(chat_worker, "enqueue_approval_notifications", chat)
    monkeypatch.setattr(
        service,
        "_partition_approvers_for_stagger",
        AsyncMock(
            return_value={
                "push_capable": [],
                "email_only": expected,
                "both_immediate": [],
                "both_stagger": [],
                "all_email": expected,
            }
        ),
    )

    await service.send_notifications(request, workflow, recipient_user_ids=override)

    push.assert_awaited_once_with(request, workflow, user_ids=expected)
    email.assert_awaited_once_with(request, workflow, user_ids=expected)
    assert chat.call_args.args[2] == expected
    assert mute.await_args.kwargs["recipient_user_ids"] == override
    assert workflow.approver_user_ids == [requester]
    if override is not None:
        resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_requesters_mute_does_not_mute_overridden_recipients(
    monkeypatch: Any,
) -> None:
    requester, colleague = uuid.uuid4(), uuid.uuid4()
    workflow = models.ApprovalWorkflow(approver_user_ids=[requester])
    service = ApprovalService(AsyncMock(), "https://app.example.com")
    lookup = AsyncMock(
        side_effect=lambda db, **kw: (
            models.ApprovalBypass(mode="mute_notifications")
            if kw["user_id"] == requester
            else None
        )
    )
    monkeypatch.setattr(service_module, "get_active_bypass_async", lookup)

    result = await service._resolve_bypass(
        workflow,
        str(uuid.uuid4()),
        None,
        "mute_notifications",
        recipient_user_ids=[colleague],
    )

    assert result is None
    assert lookup.await_args.kwargs["user_id"] == colleague


@pytest.mark.asyncio
@pytest.mark.parametrize("recipients", [[], [uuid.UUID(int=2)]])
async def test_push_fetches_devices_only_for_overridden_recipients(
    monkeypatch: Any, recipients: list[uuid.UUID]
) -> None:
    from preloop.models.crud import notification_preferences
    from preloop.models.db import session
    from preloop.services import push_notifications

    service = ApprovalService(AsyncMock(), "https://app.example.com")
    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(), approver_user_ids=[uuid.uuid4()]
    )
    prefs = Mock(return_value=None)
    monkeypatch.setattr(notification_preferences, "get_by_user", prefs)
    monkeypatch.setattr(session, "get_db_session", lambda: iter([Mock()]))
    monkeypatch.setattr(push_notifications, "get_apns_service", lambda: Mock())
    monkeypatch.setattr(
        service_module, "_operator_present_at_machine", lambda *a: False
    )

    await service._send_push_notification(
        models.ApprovalRequest(id=uuid.uuid4()), workflow, user_ids=recipients
    )

    assert [call.args[1] for call in prefs.call_args_list] == recipients
