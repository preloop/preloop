"""HTTP contract for choosing a session across console entry points."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from preloop.models.crud import crud_runtime_session


def test_finder_returns_identity_and_family_contract(
    client: Any, db_session: Session, test_user: Any
) -> None:
    """A title match uses the authenticated account and returns one tree payload."""
    now = datetime.now(UTC)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="custom",
        session_source_id="example-harness-session",
        session_reference="example-harness-session",
        runtime_principal_type="agent",
        runtime_principal_id="example-agent",
        runtime_principal_name="Example agent",
        started_at=now,
        last_activity_at=now,
    )
    response = client.post(
        "/api/v1/runtime-sessions/find", json={"query": "example-harness"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["items"]] == [str(session.id)]
    assert payload["related_sessions"] == []
    assert payload["next_offset"] is None
    assert payload["content_search"]["query"] == "example-harness"


def test_finder_rejects_unbounded_pages_and_unknown_filters(client: Any) -> None:
    """The shared selection contract rejects requests that bypass its bounds."""
    for body in (
        {"limit": 501},
        {"query": "x" * 513},
        {"account_id": "other"},
        {"query": "test", "start_date": "2026-11-01T00:00:00"},
        {"end_date": "2026-11-01T00:00:00"},
    ):
        assert (
            client.post("/api/v1/runtime-sessions/find", json=body).status_code == 422
        )
