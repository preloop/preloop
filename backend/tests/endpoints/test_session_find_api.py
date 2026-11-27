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


def test_finder_honours_the_list_workspace_filter(
    client: Any, db_session: Session, test_user: Any
) -> None:
    """The console sends its workspace filter to the finder as well as the list."""
    now = datetime.now(UTC)
    sessions = {}
    for workspace in ("workspace-a", "workspace-b"):
        session = crud_runtime_session.upsert_by_source(
            db_session,
            account_id=test_user.account_id,
            session_source_type="custom",
            session_source_id=f"example-{workspace}",
            session_reference=f"example-{workspace}",
            runtime_principal_type="agent",
            runtime_principal_id="example-agent",
            runtime_principal_name="Example agent",
            started_at=now,
            last_activity_at=now,
        )
        session.terminal_location = {
            "multiplexer": "herdr",
            "workspace_id": workspace,
            "workspace_label": workspace,
            "tab_id": f"{workspace}-tab",
            "tab_label": "Example tab",
            "pane_id": f"{workspace}-pane",
            "pane_label": "Example pane",
            "state": "idle",
            "device_id": "example-device",
            "observed_at": now.isoformat(),
        }
        sessions[workspace] = session
    db_session.commit()
    response = client.post(
        "/api/v1/runtime-sessions/find",
        json={"query": "example-workspace", "workspace_id": "workspace-a"},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        str(sessions["workspace-a"].id)
    ]
