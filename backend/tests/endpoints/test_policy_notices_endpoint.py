"""GET /api/v1/policies/notices/summary feeds the Attention card (#959)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.models.models.user import User
from preloop.services.policy_notices import PolicyNotice, record_policy_notice


def _record(db: Session, account_id, user_id, rule_id: str, at: datetime) -> str:
    outcome = record_policy_notice(
        db,
        PolicyNotice(
            account_id=account_id,
            user_id=user_id,
            target="model.response",
            rule_id=rule_id,
            rule_description=None,
            text_sha256="d" * 64,
            excerpt=f"excerpt for {rule_id}",
        ),
        now=at,
        deliver=lambda _db, _hit: {},
    )
    return str(outcome.hit_id)


def test_summary_lists_rules_with_latest_hit(
    client: TestClient, db_session: Session, test_user: User
) -> None:
    now = datetime.now(timezone.utc)
    _record(
        db_session,
        test_user.account_id,
        test_user.id,
        "notify-a",
        now - timedelta(hours=3),
    )
    latest = _record(
        db_session,
        test_user.account_id,
        test_user.id,
        "notify-a",
        now - timedelta(hours=1),
    )
    _record(
        db_session, test_user.account_id, None, "notify-b", now - timedelta(hours=2)
    )

    response = client.get("/api/v1/policies/notices/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["days"] == 7
    rules = {row["rule_id"]: row for row in body["rules"]}
    assert rules["notify-a"]["count"] == 2
    assert rules["notify-a"]["last_hit_id"] == latest
    assert rules["notify-a"]["last_excerpt"] == "excerpt for notify-a"
    assert rules["notify-a"]["last_username"] == "testuser"
    assert rules["notify-b"]["last_username"] is None
    assert body["rules"][0]["rule_id"] == "notify-a"


def test_summary_is_scoped_to_the_account(
    client: TestClient, db_session: Session, test_user: User
) -> None:
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    _record(db_session, other.id, None, "notify-elsewhere", datetime.now(timezone.utc))

    response = client.get("/api/v1/policies/notices/summary")

    assert response.status_code == 200
    assert response.json()["rules"] == []


def test_summary_window_is_bounded(client: TestClient) -> None:
    assert client.get("/api/v1/policies/notices/summary?days=0").status_code == 422
    assert client.get("/api/v1/policies/notices/summary?days=91").status_code == 422
