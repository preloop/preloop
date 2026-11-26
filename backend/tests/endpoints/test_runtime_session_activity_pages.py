"""Turn pages and search landings through the authenticated activity API."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models


def test_turn_page_and_gateway_focus_are_scoped_to_the_session(
    client: TestClient, db_session: Session, test_user: Any
) -> None:
    """An old usage hit lands directly, while foreign targets are refused."""
    at = datetime.now(UTC) - timedelta(hours=4)
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=at,
        last_activity_at=at + timedelta(minutes=60),
    )
    db_session.add(session)
    db_session.flush()
    usage_ids = [str(uuid4()) for _ in range(60)]
    rows = [
        models.RuntimeSessionActivity(
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            timestamp=at + timedelta(minutes=index),
            activity_type="model_gateway_call",
            metadata_={"api_usage_id": usage_ids[index], "total_tokens": 10},
        )
        for index in range(60)
    ]
    db_session.add_all(rows)
    db_session.flush()
    base = f"/api/v1/runtime-sessions/{session.id}"
    response = client.get(
        f"{base}/activity",
        params={"page_by_turn": True, "limit": 4, "around": usage_ids[5]},
    )
    assert response.status_code == 200, response.text
    page = response.json()
    activities = [item for item in page["items"] if item["activity_id"]]
    assert len(activities) == 4
    assert str(rows[5].id) in {item["activity_id"] for item in activities}
    assert page["has_more"]
    assert page["has_newer"]
    newer = client.get(
        f"{base}/activity",
        params={"page_by_turn": True, "limit": 4, "after": page["next_after"]},
    )
    assert newer.status_code == 200, newer.text
    newer_ids = {
        item["activity_id"] for item in newer.json()["items"] if item["activity_id"]
    }
    assert newer_ids
    assert not newer_ids & {item["activity_id"] for item in activities}
    gateway = client.get(
        f"{base}/gateway-events", params={"limit": 4, "around": usage_ids[5]}
    )
    assert gateway.status_code == 200, gateway.text
    assert str(rows[5].id) in {item["id"] for item in gateway.json()["logs"]}
    missing = client.get(
        f"{base}/activity",
        params={"page_by_turn": True, "around": str(uuid4())},
    )
    assert missing.status_code == 404


def test_foreign_activity_and_usage_targets_are_not_disclosed(
    client: TestClient,
    db_session: Session,
    test_user: Any,
) -> None:
    """IDs from another session or tenant cannot be used as focus cursors."""
    at = datetime.now(UTC)
    other = models.Account(organization_name="Example other account")
    db_session.add(other)
    db_session.flush()
    sessions = [
        models.RuntimeSession(
            account_id=account_id,
            session_source_type="test",
            session_source_id=str(uuid4()),
            started_at=at,
            last_activity_at=at,
        )
        for account_id in (test_user.account_id, test_user.account_id, other.id)
    ]
    db_session.add_all(sessions)
    db_session.flush()
    for foreign in sessions[1:]:
        usage_id = str(uuid4())
        row = models.RuntimeSessionActivity(
            account_id=foreign.account_id,
            runtime_session_id=foreign.id,
            timestamp=at,
            activity_type="model_gateway_call",
            metadata_={"api_usage_id": usage_id},
        )
        db_session.add(row)
        db_session.flush()
        for target in (str(row.id), usage_id):
            for endpoint in ("activity", "gateway-events"):
                response = client.get(
                    f"/api/v1/runtime-sessions/{sessions[0].id}/{endpoint}",
                    params={"page_by_turn": True, "around": target},
                )
                assert response.status_code == 404, response.text
            response = client.get(
                f"/api/v1/runtime-sessions/{sessions[0].id}/activity",
                params={"page_by_turn": True, "after": target},
            )
            assert response.status_code == 404, response.text


def test_identity_search_accepts_query_in_post_body(
    client: TestClient, db_session: Session, test_user: Any
) -> None:
    """Identity search keeps transcript-hunting text out of request URLs."""
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        title="Example identity needle",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    response = client.post(
        "/api/v1/runtime-sessions", json={"query": "identity needle"}
    )
    assert response.status_code == 200, response.text
    assert str(session.id) in {item["id"] for item in response.json()["items"]}
