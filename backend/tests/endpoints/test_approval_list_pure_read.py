"""The approval list is a pure read; a periodic sweep writes expiry."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_approval_request, crud_approval_workflow
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate
from preloop.sync import tasks


def _request(
    db: Session, account_id: Any, *, expires_at: datetime, label: str
) -> ApprovalRequest:
    """Store one pending approval request with the given deadline."""
    workflow = crud_approval_workflow.create(
        db,
        obj_in=ApprovalWorkflowCreate(name=f"Workflow {label}", approval_type="manual"),
        account_id=str(account_id),
    )
    db.flush()
    tool_config = ToolConfiguration(
        tool_name=f"tool_{label}",
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
        tool_name=f"tool_{label}",
        tool_args={},
        status="pending",
        requested_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        expires_at=expires_at,
        approval_token=f"token-{label}-{uuid4().hex}",
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def requests(db_session: Session, test_user: models.User) -> dict[str, Any]:
    """A stale pending request and a live one in the test user's account."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "stale": _request(
            db_session,
            test_user.account_id,
            expires_at=now - timedelta(minutes=5),
            label="stale",
        ),
        "live": _request(
            db_session,
            test_user.account_id,
            expires_at=now + timedelta(hours=1),
            label="live",
        ),
    }


def _writes(db_session: Session) -> list[str]:
    """Record every INSERT, UPDATE or DELETE run on the session's connection."""
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        """Keep data-changing statements."""
        verb = statement.lstrip().split(" ", 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", record)
    return statements


def test_list_performs_no_write_and_projects_expiry(
    client: TestClient, db_session: Session, requests: dict[str, Any]
) -> None:
    """GET writes nothing, yet a stale request reads as expired."""
    writes = _writes(db_session)

    listed = client.get("/api/v1/approval-requests")
    pending = client.get("/api/v1/approval-requests", params={"status": "pending"})
    expired = client.get("/api/v1/approval-requests", params={"status": "expired"})

    assert writes == []
    assert listed.status_code == 200, listed.text
    by_id = {row["id"]: row for row in listed.json()}
    stale, live = str(requests["stale"].id), str(requests["live"].id)
    assert by_id[stale]["status"] == "expired"
    assert by_id[stale]["resolved_at"] is not None
    assert by_id[live]["status"] == "pending"
    assert [row["id"] for row in pending.json()] == [live]
    assert pending.headers["X-Total-Count"] == "1"
    assert [row["id"] for row in expired.json()] == [stale]
    assert expired.headers["X-Total-Count"] == "1"

    db_session.refresh(requests["stale"])
    assert requests["stale"].status == "pending"


def test_periodic_sweep_writes_expiry(
    db_session: Session, requests: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduled task stores the expired status the read projected."""

    class _Shared:
        """Hand the test session to the task without closing it."""

        def __iter__(self) -> "_Shared":
            return self

        def __next__(self) -> Session:
            return db_session

    monkeypatch.setattr(tasks, "get_db_session", lambda: _Shared())
    monkeypatch.setattr(db_session, "close", lambda: None)

    assert "expire_stale_approvals" in tasks.DISPATCHABLE_TASKS
    assert asyncio.run(tasks.expire_stale_approvals()) >= 1

    db_session.refresh(requests["stale"])
    db_session.refresh(requests["live"])
    assert requests["stale"].status == "expired"
    assert requests["live"].status == "pending"
    assert (
        crud_approval_request.count_by_account(
            db_session, account_id=str(requests["stale"].account_id), status="expired"
        )
        == 1
    )
