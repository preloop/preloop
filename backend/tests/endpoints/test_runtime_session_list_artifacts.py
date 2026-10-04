"""Artifact counts and the "has artifacts" filter on the session list (#1084).

Exercised through the HTTP endpoint against the real query: counts come from
one grouped query per page, evicted artifacts are left out, and the filter
narrows the list server side.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_runtime_session_artifact as crud

SAMPLE: dict[str, tuple[str, bytes]] = {
    "screenshot": ("image/png", b"\x89PNG\r\n\x1a\nimg"),
    "transcript": ("text/vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nhallo"),
    "document": ("application/pdf", b"%PDF-1.7\nsummary"),
    "audio": ("audio/wav", b"RIFF\x24\x00\x00\x00WAVEfmt "),
}


def _session(db: Session, account_id: Any, index: int) -> models.RuntimeSession:
    now = datetime.now(UTC).replace(tzinfo=None)
    row = models.RuntimeSession(
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"artifacts-{index}-{uuid4().hex[:8]}",
        started_at=now - timedelta(hours=1),
        last_activity_at=now - timedelta(minutes=index + 1),
    )
    db.add(row)
    db.flush()
    return row


def _store(
    db: Session, session: models.RuntimeSession, kind: str
) -> models.RuntimeSessionArtifact:
    content_type, data = SAMPLE[kind]
    return crud.store(
        db,
        account_id=session.account_id,
        runtime_session_id=session.id,
        kind=kind,
        source="synthetic",
        source_ref=None,
        content_type=content_type,
        plaintext=data,
        manifest={},
        producer="deposit_api",
    )


def _items(response: Any) -> dict[str, dict[str, Any]]:
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()["items"]}


@pytest.fixture
def account_id(test_user: models.User) -> Any:
    return test_user.account_id


def test_counts_by_kind_exclude_evicted(
    client: Any, db_session: Session, account_id: Any
) -> None:
    rich = _session(db_session, account_id, 0)
    plain = _session(db_session, account_id, 1)
    _store(db_session, rich, "screenshot")
    _store(db_session, rich, "screenshot")
    _store(db_session, rich, "transcript")
    gone = _store(db_session, rich, "document")
    crud.mark_unavailable(
        db_session, account_id=account_id, artifact_id=gone.id, availability="evicted"
    )

    items = _items(client.get("/api/v1/runtime-sessions?limit=100"))

    assert items[str(rich.id)]["artifact_counts"] == {
        "screenshot": 2,
        "transcript": 1,
    }
    assert items[str(plain.id)]["artifact_counts"] == {}


def test_page_of_fifty_counts_artifacts_in_one_query(
    client: Any, db_engine: Any, db_session: Session, account_id: Any
) -> None:
    sessions = [_session(db_session, account_id, index) for index in range(50)]
    for index, session in enumerate(sessions):
        _store(db_session, session, "screenshot")
        if index % 2:
            _store(db_session, session, "transcript")

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", record)
    try:
        items = _items(client.get("/api/v1/runtime-sessions?limit=50"))
    finally:
        event.remove(db_engine, "before_cursor_execute", record)

    artifact_queries = [
        sql
        for sql in statements
        if "FROM runtime_session_artifact" in sql and "count(" in sql
    ]
    assert len(artifact_queries) == 1, artifact_queries
    assert len(items) == 50
    for index, session in enumerate(sessions):
        expected = {"screenshot": 1}
        if index % 2:
            expected["transcript"] = 1
        assert items[str(session.id)]["artifact_counts"] == expected


def test_has_artifacts_filter_narrows_the_list(
    client: Any, db_session: Session, account_id: Any
) -> None:
    shots = _session(db_session, account_id, 0)
    talk = _session(db_session, account_id, 1)
    evicted = _session(db_session, account_id, 2)
    empty = _session(db_session, account_id, 3)
    _store(db_session, shots, "screenshot")
    _store(db_session, talk, "transcript")
    gone = _store(db_session, evicted, "transcript")
    crud.mark_unavailable(
        db_session, account_id=account_id, artifact_id=gone.id, availability="evicted"
    )

    base = "/api/v1/runtime-sessions?limit=100&has_artifacts="
    assert set(_items(client.get(base + "transcript"))) == {str(talk.id)}
    assert set(_items(client.get(base + "any"))) == {str(shots.id), str(talk.id)}
    assert str(empty.id) in _items(client.get("/api/v1/runtime-sessions?limit=100"))


def test_has_artifacts_rejects_unknown_kind(client: Any) -> None:
    response = client.get("/api/v1/runtime-sessions?has_artifacts=selfie")
    assert response.status_code == 422
    assert "has_artifacts" in response.text
