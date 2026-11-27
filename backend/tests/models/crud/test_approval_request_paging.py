"""Server paging, filters, total and stats of the approvals list (#1585).

The approvals page used to page the whole account history into the browser
to count and search it. These run against Postgres so the COUNT, the text
search and the FILTER aggregates are the real SQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_approval_event, crud_approval_request


@pytest.fixture
def account(db_session: Session):
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": f"approvals-{uuid.uuid4().hex[:8]}",
            "is_active": True,
            "meta_data": {},
        },
    )
    tool = models.ToolConfiguration(
        id=uuid.uuid4(),
        tool_name="bash",
        tool_source="mcp",
        is_enabled=True,
        account_id=account.id,
    )
    workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account.id,
        name="Default",
        approval_type="standard",
        require_reason=False,
        is_default=True,
        workflow_type="standard",
    )
    db_session.add_all([tool, workflow])
    db_session.flush()
    now = datetime.utcnow()
    rows = [
        # (tool, status, minutes to decide, bypass, ai, summary)
        ("bash", "pending", None, None, False, "Run 50% of tests"),
        ("bash", "approved", 10, None, False, "Deploy staging"),
        ("bash", "approved", 30, "bypass", False, "Bypassed"),
        ("create_pr", "approved", 20, None, True, "AI decided"),
        ("create_pr", "declined", 20, None, False, "Open a PR"),
        ("send_email", "expired", None, None, False, "Mail the team"),
        ("send_email", "cancelled", None, None, False, "Mail again"),
    ]
    for index, (tool_name, status, minutes, bypass, ai, summary) in enumerate(rows):
        requested = now - timedelta(hours=index + 1)
        db_session.add(
            models.ApprovalRequest(
                id=uuid.uuid4(),
                account_id=account.id,
                tool_configuration_id=tool.id,
                approval_workflow_id=workflow.id,
                tool_name=tool_name,
                tool_args={"command": f"step-{index}"},
                status=status,
                requested_at=requested,
                resolved_at=requested + timedelta(minutes=minutes) if minutes else None,
                approval_token=uuid.uuid4().hex,
                auto_approved_reason=bypass,
                decided_by_ai=ai,
                summary=summary,
            )
        )
    db_session.flush()
    return account


def test_count_matches_the_paged_filter(db_session: Session, account) -> None:
    account_id = str(account.id)
    assert (
        crud_approval_request.count_by_account(db_session, account_id=account_id) == 7
    )
    page = crud_approval_request.get_multi_by_account(
        db_session, account_id=account_id, skip=2, limit=2
    )
    assert len(page) == 2
    assert (
        crud_approval_request.count_by_account(
            db_session, account_id=account_id, tool_name="create_pr"
        )
        == 2
    )
    assert (
        crud_approval_request.count_by_account(
            db_session, account_id=account_id, status="approved", tool_name="bash"
        )
        == 2
    )


def test_search_matches_summary_and_arguments_literally(
    db_session: Session, account
) -> None:
    account_id = str(account.id)
    found = crud_approval_request.get_multi_by_account(
        db_session, account_id=account_id, search="MAIL"
    )
    assert {row.status for row in found} == {"expired", "cancelled"}
    by_args = crud_approval_request.get_multi_by_account(
        db_session, account_id=account_id, search="step-3"
    )
    assert [row.summary for row in by_args] == ["AI decided"]
    # % is a literal, not a wildcard.
    literal = crud_approval_request.get_multi_by_account(
        db_session, account_id=account_id, search="50%"
    )
    assert [row.summary for row in literal] == ["Run 50% of tests"]
    assert (
        crud_approval_request.count_by_account(
            db_session, account_id=account_id, search="%"
        )
        == 1
    )


def test_stats_cover_the_whole_account(db_session: Session, account) -> None:
    rows = crud_approval_request.get_multi_by_account(
        db_session, account_id=str(account.id)
    )
    approved = next(row for row in rows if row.summary == "Deploy staging")
    approved.responses = [{"user_id": "anonymous", "decision": "approved"}]
    declined = next(row for row in rows if row.status == "declined")
    crud_approval_event.record(
        db_session,
        account_id=account.id,
        approval_request_id=declined.id,
        event_type="vote_received",
        detail="Token decision",
    )
    stats = crud_approval_request.stats_by_account(
        db_session, account_id=str(account.id)
    )
    assert stats["total"] == 7
    assert stats["pending"] == 1
    assert stats["approved"] == 3
    assert stats["declined"] == 1
    assert stats["expired"] == 1
    assert stats["cancelled"] == 1
    # Bypass and AI decisions are not a person approving.
    assert stats["human_approved"] == 1
    assert stats["human_declined"] == 1
    assert stats["auto_approved_by_bypass"] == 1
    # Mean of 10, 30, 20 and 20 minutes over approved and declined.
    assert stats["avg_response_minutes"] == 20
    assert stats["tools"] == ["bash", "create_pr", "send_email"]


def test_human_stats_require_positive_vote_evidence(
    db_session: Session, account: models.Account
) -> None:
    """Terminal status and missing automatic fields do not establish a person."""
    rows = crud_approval_request.get_multi_by_account(
        db_session, account_id=str(account.id)
    )
    for row in rows:
        row.responses = [
            {"decision": row.status},
            {"user_id": "", "decision": row.status},
            {"user_id": False, "decision": row.status},
        ]
    db_session.flush()
    stats = crud_approval_request.stats_by_account(
        db_session, account_id=str(account.id)
    )
    assert stats["human_approved"] == 0
    assert stats["human_declined"] == 0


def test_delivery_warning_survives_expiry_and_is_account_scoped(
    db_session: Session, account: models.Account
) -> None:
    """The warning includes history even when the pending page is empty."""
    account_id = str(account.id)
    assert not crud_approval_request.stats_by_account(
        db_session, account_id=account_id
    )["has_native_delivery_warning"]
    rows = crud_approval_request.get_multi_by_account(
        db_session, account_id=account_id, status="expired"
    )
    crud_approval_request.update(
        db_session,
        db_obj=rows[0],
        obj_in={"tool_args": {"_preloop_native_delivery_warning": "Email only"}},
    )
    assert crud_approval_request.stats_by_account(db_session, account_id=account_id)[
        "has_native_delivery_warning"
    ]
    assert not crud_approval_request.stats_by_account(
        db_session, account_id=str(uuid.uuid4())
    )["has_native_delivery_warning"]
