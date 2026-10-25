"""Audit page totals describe the matching population, independent of pagination."""

from datetime import datetime, timedelta, timezone, tzinfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_audit_log, crud_user


def test_resource_type_total_matches_filtered_pages(
    audit_client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Resource filtering affects both the returned page and its total."""
    expected_ids = set()
    for resource_type, count in [("session_search", 3), ("tool", 4)]:
        for _ in range(count):
            row = crud_audit_log.log_action(
                db_session,
                account_id=test_user.account_id,
                user_id=test_user.id,
                action="pagination_probe",
                resource_type=resource_type,
                status="success",
            )
            if resource_type == "session_search":
                expected_ids.add(str(row.id))

    seen_ids = set()
    for skip in [0, 1, 2, 3]:
        response = audit_client.get(
            "/api/v1/audit-logs",
            params={
                "action": "pagination_probe",
                "resource_type": "session_search",
                "skip": skip,
                "limit": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert len(data["logs"]) == (1 if skip < 3 else 0)
        seen_ids.update(row["id"] for row in data["logs"])
    assert seen_ids == expected_ids


@pytest.mark.parametrize("skip, page_size", [(0, 1), (1, 1), (3, 0)])
def test_user_total_respects_account_user_and_shared_cutoff(
    audit_client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    skip: int,
    page_size: int,
) -> None:
    """A full, partial or empty page reports the same scoped user total."""
    from preloop.api.endpoints import audit

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return now

    monkeypatch.setattr(audit, "datetime", FrozenDatetime)
    target = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "audit-pagination@example.invalid",
            "username": "audit-pagination",
            "hashed_password": "synthetic",
            "is_active": True,
        },
    )
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other audit account"}
    )
    expected_ids = set()
    for account_id, user_id, timestamp, included in [
        (test_user.account_id, target.id, now, True),
        (test_user.account_id, target.id, now - timedelta(days=1), True),
        (test_user.account_id, target.id, cutoff, True),
        (test_user.account_id, target.id, cutoff - timedelta(seconds=1), False),
        (test_user.account_id, test_user.id, now, False),
        (foreign.id, target.id, now, False),
    ]:
        row = crud_audit_log.log_action(
            db_session,
            account_id=account_id,
            user_id=user_id,
            action="pagination_probe",
            status="success",
        )
        crud_audit_log.update(db_session, db_obj=row, obj_in={"timestamp": timestamp})
        if included:
            expected_ids.add(str(row.id))

    response = audit_client.get(
        f"/api/v1/audit-logs/user/{target.id}",
        params={"days": 30, "skip": skip, "limit": 1},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["logs"]) == page_size
    assert all(row["id"] in expected_ids for row in data["logs"])
