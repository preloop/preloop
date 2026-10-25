"""Keyset paging of a runtime session's gateway events (Talk "load older")."""

import uuid
from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_runtime_session, crud_runtime_session_activity


def _session(db_session, test_user, key: str):
    now = datetime.now(UTC)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=key,
        runtime_principal_type="claude_code",
        runtime_principal_id=key,
        runtime_principal_name="Workspace",
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    return session


def _log(db_session, test_user, session, when: datetime, label: str) -> str:
    activity = crud_runtime_session_activity.log_model_gateway_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        status="success",
        timestamp=when,
        metadata={"model_alias": "openai/gpt-5.5", "label": label},
    )
    db_session.commit()
    return str(activity.id)


def _labels(body: dict) -> list[str]:
    return [event["payload"]["label"] for event in body["logs"]]


def test_cursor_pages_do_not_shift_when_new_events_arrive(
    client, db_session, test_user
):
    """A page read by cursor neither repeats nor skips rows after new events."""
    base = datetime.now(UTC) - timedelta(minutes=10)
    session = _session(db_session, test_user, "cursor-shift")
    for index in range(5):
        _log(
            db_session, test_user, session, base + timedelta(seconds=index), f"e{index}"
        )
    url = f"/api/v1/runtime-sessions/{session.id}/gateway-events"

    first = client.get(f"{url}?limit=2").json()
    assert _labels(first) == ["e4", "e3"]
    cursor = first["pagination"]["next_cursor"]
    assert cursor == first["logs"][-1]["id"]

    # Two calls land at the head while the operator reads back. With offset
    # paging the next page (offset=2) would repeat e4 and e3.
    for index in (5, 6):
        _log(
            db_session, test_user, session, base + timedelta(seconds=index), f"e{index}"
        )

    second = client.get(f"{url}?limit=2&before={cursor}").json()
    assert _labels(second) == ["e2", "e1"]
    assert second["pagination"]["has_more"] is True
    assert second["pagination"]["next_offset"] is None

    third = client.get(
        f"{url}?limit=2&before={second['pagination']['next_cursor']}"
    ).json()
    assert _labels(third) == ["e0"]
    assert third["pagination"]["has_more"] is False
    assert third["pagination"]["next_cursor"] is None


def test_cursor_breaks_timestamp_ties_by_id(client, db_session, test_user):
    """Events sharing a timestamp are each returned exactly once."""
    when = datetime.now(UTC) - timedelta(minutes=5)
    session = _session(db_session, test_user, "cursor-ties")
    for index in range(4):
        _log(db_session, test_user, session, when, f"t{index}")
    url = f"/api/v1/runtime-sessions/{session.id}/gateway-events"

    seen: list[str] = []
    body = client.get(f"{url}?limit=1").json()
    seen += [event["id"] for event in body["logs"]]
    while body["pagination"]["has_more"]:
        body = client.get(
            f"{url}?limit=1&before={body['pagination']['next_cursor']}"
        ).json()
        seen += [event["id"] for event in body["logs"]]
    assert len(seen) == 4
    assert len(set(seen)) == 4


def test_cursor_must_name_an_event_of_this_session(client, db_session, test_user):
    """An unknown or foreign cursor is a 404; a malformed one is a 422."""
    session = _session(db_session, test_user, "cursor-own")
    other = _session(db_session, test_user, "cursor-other")
    foreign = _log(db_session, test_user, other, datetime.now(UTC), "x")
    url = f"/api/v1/runtime-sessions/{session.id}/gateway-events"

    assert client.get(f"{url}?before={uuid.uuid4()}").status_code == 404
    assert client.get(f"{url}?before={foreign}").status_code == 404
    assert client.get(f"{url}?before=not-an-id").status_code == 422
