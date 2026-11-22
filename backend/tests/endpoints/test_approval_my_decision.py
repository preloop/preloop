"""Approval responses carry the caller's own vote as ``my_decision``.

With several approvers a single Deny (or Dismiss on a question, which is a
decline) is recorded as one vote and the request stays pending for the
others. ``my_decision`` lets the console stop asking the person who already
voted, without hiding the request from anyone else.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import crud_approval_workflow, crud_user
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate
from preloop.services.approval_service import ApprovalService


def _colleague(db: Session, account_id: Any) -> models.User:
    """Store a second user in the same account."""
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": "jane.doe@example.com",
            "username": "janedoe",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )
    db.flush()
    return user


def _pending_with_vote(
    db: Session, account_id: Any, voter_id: Any, decision: str
) -> ApprovalRequest:
    """Store a pending question that one approver has already voted on."""
    workflow = crud_approval_workflow.create(
        db,
        obj_in=ApprovalWorkflowCreate(
            name=f"Two approvers {uuid4().hex[:8]}", approval_type="manual"
        ),
        account_id=str(account_id),
    )
    db.flush()
    tool_config = ToolConfiguration(
        tool_name=f"ask_user_{uuid4().hex[:8]}",
        tool_source="builtin",
        account_id=account_id,
        approval_workflow_id=workflow.id,
    )
    db.add(tool_config)
    db.flush()
    row = ApprovalRequest(
        account_id=account_id,
        tool_configuration_id=tool_config.id,
        approval_workflow_id=workflow.id,
        tool_name="ask_user",
        tool_args={"question": "Ship it?"},
        status="pending",
        requested_at=datetime.now(UTC).replace(tzinfo=None),
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
        approval_token=f"token-{uuid4().hex}",
        responses=[
            {
                "user_id": str(voter_id),
                "decision": decision,
                "comment": None,
                "timestamp": "2026-01-01T00:00:00",
            }
        ],
    )
    db.add(row)
    db.flush()
    return row


def test_voter_sees_own_decline_and_colleague_does_not(
    app: Any,
    client: TestClient,
    db_session: Session,
    test_user: models.User,
) -> None:
    """The vote is scoped to the voter: others still see an open request."""
    row = _pending_with_vote(db_session, test_user.account_id, test_user.id, "declined")
    colleague = _colleague(db_session, test_user.account_id)

    listed = client.get("/api/v1/approval-requests", params={"status": "pending"})
    assert listed.status_code == 200, listed.text
    mine = next(r for r in listed.json() if r["id"] == str(row.id))
    assert mine["status"] == "pending"
    assert mine["my_decision"] == "declined"
    detail = client.get(f"/api/v1/approval-requests/{row.id}")
    assert detail.json()["my_decision"] == "declined"

    app.dependency_overrides[get_current_active_user] = lambda: crud_user.get(
        db_session, id=colleague.id
    )
    theirs = client.get("/api/v1/approval-requests", params={"status": "pending"})
    other = next(r for r in theirs.json() if r["id"] == str(row.id))
    assert other["status"] == "pending"
    assert other["my_decision"] is None


def test_decline_response_reports_the_recorded_vote(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """A decline that only records a vote says so instead of looking ignored."""
    row = _pending_with_vote(db_session, test_user.account_id, test_user.id, "declined")
    service = MagicMock()
    service.get_approval_request = AsyncMock(
        return_value=MagicMock(
            account_id=test_user.account_id, status="pending", tool_name="ask_user"
        )
    )
    service.decline_request = AsyncMock(return_value=row)
    base = "preloop.api.endpoints.approval_requests"
    with (
        patch(f"{base}.ApprovalService", return_value=service),
        patch(f"{base}.get_async_db_session") as get_session,
        patch(f"{base}.attributed_async", new=AsyncMock(side_effect=lambda _, r: r)),
        patch(f"{base}._reject_managed_publication_decision"),
        patch(f"{base}._reject_managed_maintenance_decision"),
        patch(f"{base}._advance_security_maintenance", new=AsyncMock()),
    ):
        get_session.return_value.__aenter__.return_value = MagicMock()
        response = client.post(f"/api/v1/approval-requests/{row.id}/decline")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["my_decision"] == "declined"


@pytest.mark.asyncio
async def test_one_of_two_approvers_declining_records_a_vote() -> None:
    """Quorum 1 of 2 named approvers: a decline persists and stays pending."""
    voter, other = uuid4(), uuid4()
    workflow = MagicMock(
        approvals_required=1, approver_user_ids=[voter, other], approver_team_ids=None
    )
    request = MagicMock(
        id=uuid4(),
        account_id=uuid4(),
        status="pending",
        responses=[],
        approval_workflow=workflow,
    )
    db = MagicMock(commit=AsyncMock(), refresh=AsyncMock())
    service = ApprovalService(db, "http://localhost")
    with (
        patch.object(
            service,
            "get_approval_request_for_update",
            new=AsyncMock(return_value=request),
        ),
        patch.object(service, "_require_consent_owner", new=AsyncMock()),
        patch.object(
            service, "_reject_if_not_actionable", new=AsyncMock(return_value=None)
        ),
        patch.object(
            service, "_external_decision_fields", new=AsyncMock(return_value={})
        ),
        patch.object(service, "_record_event", new=AsyncMock()),
        patch.object(service, "_broadcast_approval_update", new=AsyncMock()),
        patch.object(service, "update_approval_request", new=AsyncMock()) as update,
    ):
        result = await service.decline_request(request.id, None, user_id=voter)

    update.assert_not_called()
    db.commit.assert_awaited()
    assert result.status == "pending"
    assert result.responses == [
        {
            "user_id": str(voter),
            "decision": "declined",
            "comment": None,
            "timestamp": result.responses[0]["timestamp"],
        }
    ]


def test_awaiting_me_filters_voted_rows_before_the_page_limit(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """A full page of voted rows cannot hide an older request still for me."""
    voted = [
        _pending_with_vote(db_session, test_user.account_id, test_user.id, "declined")
        for _ in range(2)
    ]
    older = _pending_with_vote(db_session, test_user.account_id, uuid4(), "declined")
    older.requested_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    db_session.flush()

    page = client.get(
        "/api/v1/approval-requests",
        params={"status": "pending", "awaiting_me": "true", "limit": 2},
    )
    assert page.status_code == 200, page.text
    assert [r["id"] for r in page.json()] == [str(older.id)]
    assert page.headers["X-Total-Count"] == "1"

    everything = client.get("/api/v1/approval-requests", params={"status": "pending"})
    ids = {r["id"] for r in everything.json()}
    assert {str(v.id) for v in voted} | {str(older.id)} <= ids
