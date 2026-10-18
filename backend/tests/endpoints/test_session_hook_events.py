"""Prompt, turn-end and session-end timestamps from the harness hooks (#1889).

The hooks already run on every onboarded harness; these tests pin what is
persisted from them (a kind and a time, never prompt text), the dedupe that
keeps a doubled hook from counting twice, and that an unknown session never
fails the harness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
from sqlalchemy.orm import Session, sessionmaker

from preloop.api.endpoints import agent_permission as endpoint
from preloop.models.crud import crud_runtime_session_activity
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.runtime_session_activity import RuntimeSessionActivity
from preloop.services import session_hook_events

TOKEN_URL = "/api/v1/auth/runtime-sessions/token"
START_URL = "/api/v1/agents/session-start"
EVENT_URL = "/api/v1/agents/session-event"
SECRET_PROMPT = "deploy with password hunter2 to prod"


@pytest.fixture(autouse=True)
def worker_sessions(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Routes that own their Session still share the test's transaction."""
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(
        "preloop.api.endpoints.agent_permission.get_session_factory",
        lambda: factory,
    )


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_hook_events, "emit_account_event", events.append)
    return events


@pytest.fixture
def token(client: Any) -> str:
    response = client.post(
        TOKEN_URL,
        json={
            "session_source_type": "claude_code",
            "session_source_id": "laptop",
            "session_reference": "/repo",
            "runtime_principal_name": "Laptop Claude Code",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows(db: Session, session_id: Any, kind: str) -> list[RuntimeSessionActivity]:
    return (
        db.query(RuntimeSessionActivity)
        .filter(
            RuntimeSessionActivity.runtime_session_id == session_id,
            RuntimeSessionActivity.activity_type == kind,
        )
        .all()
    )


def _start(client: Any, token: str, **body: Any) -> dict[str, Any]:
    response = client.post(
        START_URL,
        json={"session_id": "ext-1", "source": "claude_code", **body},
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_prompt_writes_one_row_without_text(
    client: Any, db_session: Session, token: str, test_user: Any, emitted: list
) -> None:
    started = _start(client, token)
    before = db_session.get(RuntimeSession, started["runtime_session_id"])
    before_activity = before.last_activity_at

    _start(
        client, token, hook_event_name="UserPromptSubmit", first_prompt=SECRET_PROMPT
    )

    rows = _rows(db_session, started["runtime_session_id"], "user_prompt")
    assert len(rows) == 1
    row = rows[0]
    assert row.summary is None
    assert SECRET_PROMPT not in str(row.metadata_)
    assert "hunter2" not in str(row.metadata_)
    assert row.metadata_["actor_user_id"] == str(test_user.id)
    assert row.metadata_["hook_event_name"] == "UserPromptSubmit"
    assert row.metadata_["managed_agent_id"]
    assert row.api_key_id is not None
    db_session.expire_all()
    after = db_session.get(RuntimeSession, started["runtime_session_id"])
    assert after.last_activity_at >= before_activity
    assert [e["type"] for e in emitted] == ["runtime_session_prompt"]
    assert emitted[0]["topic"] == "runtime_sessions"
    assert emitted[0]["payload"]["actor_user_id"] == str(test_user.id)


def test_session_start_writes_no_prompt_row(
    client: Any, db_session: Session, token: str, emitted: list
) -> None:
    started = _start(client, token, hook_event_name="SessionStart")
    assert _rows(db_session, started["runtime_session_id"], "user_prompt") == []
    started_legacy = _start(client, token)
    assert started_legacy["runtime_session_id"] == started["runtime_session_id"]
    assert _rows(db_session, started["runtime_session_id"], "user_prompt") == []
    assert emitted == []


def test_cursor_prompt_counts_as_a_prompt(
    client: Any, db_session: Session, token: str
) -> None:
    started = _start(client, token, hook_event_name="beforeSubmitPrompt")
    assert len(_rows(db_session, started["runtime_session_id"], "user_prompt")) == 1


def test_stop_writes_turn_end_once_within_two_seconds(
    client: Any, db_session: Session, token: str, emitted: list
) -> None:
    started = _start(client, token)
    for _ in range(2):
        response = client.post(
            EVENT_URL,
            json={"session_id": "ext-1", "hook_event_name": "Stop"},
            headers=_auth(token),
        )
        assert response.status_code == 204, response.text
    rows = _rows(db_session, started["runtime_session_id"], "turn_end")
    assert len(rows) == 1
    assert rows[0].summary is None
    assert rows[0].metadata_["subagent"] is False
    assert [e["type"] for e in emitted] == ["runtime_session_turn_ended"]


def test_subagent_stop_is_marked(client: Any, db_session: Session, token: str) -> None:
    started = _start(client, token)
    client.post(
        EVENT_URL,
        json={"session_id": "ext-1", "hook_event_name": "SubagentStop"},
        headers=_auth(token),
    )
    rows = _rows(db_session, started["runtime_session_id"], "turn_end")
    assert rows[0].metadata_["subagent"] is True


def test_unknown_session_is_204_and_writes_nothing(
    client: Any, db_session: Session, token: str
) -> None:
    response = client.post(
        EVENT_URL,
        json={"session_id": "never-registered", "hook_event_name": "Stop"},
        headers=_auth(token),
    )
    assert response.status_code == 204
    assert (
        db_session.query(RuntimeSessionActivity)
        .filter(RuntimeSessionActivity.activity_type == "turn_end")
        .count()
        == 0
    )


def test_session_event_requires_a_bearer(client: Any) -> None:
    response = client.post(
        EVENT_URL, json={"session_id": "ext-1", "hook_event_name": "Stop"}
    )
    assert response.status_code == 401


def test_session_event_rejects_unknown_hook_names(client: Any, token: str) -> None:
    response = client.post(
        EVENT_URL,
        json={"session_id": "ext-1", "hook_event_name": "PreToolUse"},
        headers=_auth(token),
    )
    assert response.status_code == 422


def test_session_end_sets_ended_at_once_and_never_reopens(
    client: Any, db_session: Session, token: str
) -> None:
    started = _start(client, token)
    response = client.post(
        EVENT_URL,
        json={"session_id": "ext-1", "hook_event_name": "SessionEnd"},
        headers=_auth(token),
    )
    assert response.status_code == 204
    db_session.expire_all()
    session = db_session.get(RuntimeSession, started["runtime_session_id"])
    first_end = session.ended_at
    assert first_end is not None
    assert len(_rows(db_session, session.id, "session_end")) == 1

    # Pretend the second SessionEnd arrives later: ended_at stays.
    for row in _rows(db_session, session.id, "session_end"):
        row.timestamp = row.timestamp - timedelta(seconds=10)
    db_session.flush()
    client.post(
        EVENT_URL,
        json={"session_id": "ext-1", "hook_event_name": "sessionEnd"},
        headers=_auth(token),
    )
    db_session.expire_all()
    session = db_session.get(RuntimeSession, started["runtime_session_id"])
    assert session.ended_at == first_end


def test_crud_dedupe_is_per_kind_and_window(
    db_session: Session, test_user: Any
) -> None:
    session = RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="dedupe",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    now = datetime.now(UTC)
    common = dict(account_id=test_user.account_id, runtime_session_id=session.id)
    first = crud_runtime_session_activity.log_hook_turn_event(
        db_session, activity_type="turn_end", timestamp=now, **common
    )
    repeat = crud_runtime_session_activity.log_hook_turn_event(
        db_session,
        activity_type="turn_end",
        timestamp=now + timedelta(seconds=1),
        **common,
    )
    other_kind = crud_runtime_session_activity.log_hook_turn_event(
        db_session,
        activity_type="user_prompt",
        timestamp=now + timedelta(seconds=1),
        **common,
    )
    later = crud_runtime_session_activity.log_hook_turn_event(
        db_session,
        activity_type="turn_end",
        timestamp=now + timedelta(seconds=3),
        **common,
    )
    assert first is not None and repeat is None
    assert other_kind is not None and later is not None
    with pytest.raises(ValueError):
        crud_runtime_session_activity.log_hook_turn_event(
            db_session, activity_type="tool_call", **common
        )


def test_request_schemas_carry_no_prompt_field_for_events() -> None:
    """Privacy: the event route has no field that could carry content."""
    fields = set(endpoint.AgentSessionEventRequest.model_fields)
    assert fields == {"session_id", "hook_event_name", "source"}
    for banned in ("prompt", "first_prompt", "summary", "tool_args"):
        assert banned not in fields
    names = set(
        get_args(
            endpoint.AgentSessionEventRequest.model_fields["hook_event_name"].annotation
        )
    )
    assert names == {"Stop", "SubagentStop", "SessionEnd", "stop", "sessionEnd"}
