"""Endpoint tests for operator notes: send, list, cancel, and the hook pull.

Sending a note is a governed action with the same permission as the kill
switch, so these tests pin the authorization, the account boundary, the rate
limit and the record, not just the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.crud import (
    crud_account,
    crud_agent_control_command,
    crud_audit_log,
    crud_managed_agent,
)
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.managed_agent import ManagedAgent
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.webhook_endpoint import (
    SOURCE_ACCOUNT,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services import operator_notes
from preloop.services.event_webhooks.signing import generate_secret, secret_hint
from preloop.utils.encryption import encrypt_value

NOTES_URL = "/api/v1/operator-notes"
PENDING_URL = "/api/v1/agents/notes/pending"
SESSION_END_URL = "/api/v1/agents/session-end"
TOKEN_URL = "/api/v1/auth/runtime-sessions/token"
PERMISSION_CHECK_URL = "/api/v1/agents/permission-check"


@pytest.fixture(autouse=True)
def worker_sessions(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Routes that own their Session still share the test's transaction."""
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    for module in (
        "preloop.api.endpoints.operator_notes",
        "preloop.api.endpoints.agent_permission",
    ):
        monkeypatch.setattr(f"{module}.get_session_factory", lambda: factory)


@pytest.fixture
def agent(db_session, test_user):
    return crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Unattended Worker",
        commit=True,
    )


@pytest.fixture
def runtime_session(db_session, test_user, agent):
    session = RuntimeSession(
        id=uuid4(),
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-1",
        session_reference="/repo",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    agent.runtime_session_id = session.id
    db_session.flush()
    return session


# --- sending ----------------------------------------------------------------


def test_send_note_to_an_agent_returns_it_pending(client, agent, runtime_session):
    """A note starts pending, addressed to the agent's live session."""
    response = client.post(
        NOTES_URL,
        json={"agent_id": str(agent.id), "text": "Stop after the current test."},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "pending"
    assert body["text"] == "Stop after the current test."
    assert body["managed_agent_id"] == str(agent.id)
    assert body["runtime_session_id"] == str(runtime_session.id)
    assert body["author"]["display"] == "Test User"
    assert body["author"]["auth_method"] == "session"
    assert body["delivered_at"] is None
    assert body["expires_at"] is not None


def test_send_note_to_one_session_only_targets_that_session(
    client, db_session, test_user, agent, runtime_session
):
    """A session-addressed note names that session and no other."""
    other = RuntimeSession(
        id=uuid4(),
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-2",
        started_at=datetime.now(UTC),
    )
    db_session.add(other)
    db_session.flush()

    response = client.post(
        NOTES_URL,
        json={"runtime_session_id": str(other.id), "text": "Only you."},
    )

    assert response.status_code == 201, response.text
    assert response.json()["runtime_session_id"] == str(other.id)


def test_note_body_and_target_are_validated(client, agent, runtime_session):
    """Exactly one target, and a body that is neither empty nor unbounded."""
    too_many = client.post(
        NOTES_URL,
        json={
            "agent_id": str(agent.id),
            "runtime_session_id": str(runtime_session.id),
            "text": "hi",
        },
    )
    assert too_many.status_code == 422

    none_at_all = client.post(NOTES_URL, json={"text": "hi"})
    assert none_at_all.status_code == 422

    empty = client.post(NOTES_URL, json={"agent_id": str(agent.id), "text": ""})
    assert empty.status_code == 422

    huge = client.post(
        NOTES_URL,
        json={
            "agent_id": str(agent.id),
            "text": "x" * (operator_notes.MAX_NOTE_BODY_CHARS + 1),
        },
    )
    assert huge.status_code == 422


def test_a_note_can_never_be_addressed_to_another_account(client, db_session):
    """A foreign agent id is a 404, never a cross-account delivery."""
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    foreign_agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=other_account.id,
        display_name="Their Worker",
        commit=True,
    )

    response = client.post(
        NOTES_URL,
        json={"agent_id": str(foreign_agent.id), "text": "leak"},
    )

    assert response.status_code == 404
    assert (
        crud_agent_control_command.list_notes(
            db_session, account_id=other_account.id, managed_agent_id=foreign_agent.id
        )
        == []
    )


def test_unknown_execution_is_a_404(client):
    """An execution with no governed call yet has no session to steer."""
    response = client.post(
        NOTES_URL,
        json={"execution_id": str(uuid4()), "text": "hi"},
    )
    assert response.status_code == 404
    assert "runtime session" in response.json()["detail"]


def test_rate_limit_stops_one_author_flooding_one_agent(client, agent, monkeypatch):
    """A note channel that bursts is a channel nobody reads."""
    monkeypatch.setattr(
        "preloop.api.endpoints.operator_notes.NOTE_RATE_LIMIT_PER_HOUR", 3
    )
    for index in range(3):
        assert (
            client.post(
                NOTES_URL, json={"agent_id": str(agent.id), "text": f"note {index}"}
            ).status_code
            == 201
        )

    response = client.post(NOTES_URL, json={"agent_id": str(agent.id), "text": "more"})
    assert response.status_code == 429
    assert "Rate limit reached" in response.json()["detail"]


def test_rate_limit_covers_a_session_with_no_managed_agent(
    client, db_session, test_user, monkeypatch
):
    """A flow session on an account credential is still rate-limited."""
    session = RuntimeSession(
        id=uuid4(),
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="credential-flow",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    monkeypatch.setattr(
        "preloop.api.endpoints.operator_notes.NOTE_RATE_LIMIT_PER_HOUR", 3
    )
    for index in range(3):
        assert (
            client.post(
                NOTES_URL,
                json={
                    "runtime_session_id": str(session.id),
                    "text": f"note {index}",
                },
            ).status_code
            == 201
        )

    response = client.post(
        NOTES_URL,
        json={"runtime_session_id": str(session.id), "text": "more"},
    )
    assert response.status_code == 429
    assert "Rate limit reached" in response.json()["detail"]


def test_sending_is_audited_and_evented(client, db_session, test_user, agent):
    """The human decision is recorded before the author is told it worked."""
    secret = generate_secret()
    endpoint = WebhookEndpoint(
        account_id=test_user.account_id,
        url="https://example.com/hook",
        secret_encrypted=encrypt_value(secret),
        secret_hint=secret_hint(secret),
        event_types=["agent.note_sent"],
        active=True,
        source=SOURCE_ACCOUNT,
    )
    db_session.add(endpoint)
    db_session.flush()

    response = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Prefer the smaller diff."}
    )
    note_id = response.json()["note_id"]

    entries = crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        action=operator_notes.AUDIT_NOTE_SENT,
    )
    assert len(entries) == 1
    assert entries[0].user_id == test_user.id
    assert entries[0].resource_type == "operator_note"
    assert entries[0].resource_id == note_id
    assert entries[0].details["body_chars"] == len("Prefer the smaller diff.")

    deliveries = (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == test_user.account_id)
        .all()
    )
    assert len(deliveries) == 1
    assert deliveries[0].payload["type"] == "agent.note_sent"
    assert deliveries[0].payload["data"]["note_id"] == note_id


# --- listing and cancelling -------------------------------------------------


def test_list_notes_for_an_agent_newest_first(client, agent, runtime_session):
    """The composer needs the recent notes and their delivery state."""
    client.post(NOTES_URL, json={"agent_id": str(agent.id), "text": "first"})
    client.post(NOTES_URL, json={"agent_id": str(agent.id), "text": "second"})

    response = client.get(NOTES_URL, params={"agent_id": str(agent.id)})

    assert response.status_code == 200, response.text
    notes = response.json()["notes"]
    assert [note["text"] for note in notes] == ["second", "first"]
    assert all(note["state"] == "pending" for note in notes)


def test_send_and_list_by_execution_resolve_the_same_session(
    client, db_session, test_user, agent, runtime_session
):
    """Both routes reach an execution through its newest governed call."""
    from preloop.models import models
    from preloop.models.models.api_usage import ApiUsage

    flow = models.Flow(
        account_id=test_user.account_id,
        name="Example flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        ApiUsage(
            account_id=test_user.account_id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.2,
            flow_execution_id=execution.id,
            runtime_session_id=runtime_session.id,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db_session.flush()

    sent = client.post(
        NOTES_URL,
        json={"execution_id": str(execution.id), "text": "Your base branch moved."},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["runtime_session_id"] == str(runtime_session.id)

    listed = client.get(NOTES_URL, params={"execution_id": str(execution.id)})
    assert listed.status_code == 200, listed.text
    assert [note["text"] for note in listed.json()["notes"]] == [
        "Your base branch moved."
    ]


def test_a_note_from_a_person_records_a_user_author_not_an_agent(
    client, db_session, agent
):
    """The author fields are exclusive: a person's note names no agent."""
    body = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Stop after this test."}
    ).json()

    assert body["author"]["user_id"] is not None
    assert body["author"]["agent_id"] is None
    assert body["author"]["auth_method"] == "session"
    stored = crud_agent_control_command.get_note(
        db_session, account_id=agent.account_id, note_id=body["note_id"]
    )
    assert stored.created_by_managed_agent_id is None


def test_list_notes_requires_a_target(client):
    """Listing every note in an account is not a question anyone asks."""
    assert client.get(NOTES_URL).status_code == 400


def test_cancel_withdraws_a_pending_note(client, db_session, agent):
    """Cancelling is a state, and the withdrawn note is never delivered."""
    note_id = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "never mind"}
    ).json()["note_id"]

    response = client.post(f"{NOTES_URL}/{note_id}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cancelled"
    assert response.json()["cancelled_at"] is not None
    stored = crud_agent_control_command.get_note(
        db_session, account_id=agent.account_id, note_id=note_id
    )
    assert stored.status == "cancelled"


def test_cancel_of_an_unknown_note_is_a_404(client):
    assert client.post(f"{NOTES_URL}/nope/cancel").status_code == 404


# --- authorization ----------------------------------------------------------


def test_sending_requires_the_agent_control_permission(db_session, test_viewer_user):
    """A viewer cannot steer an agent, exactly as it cannot stop one."""
    viewer_agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_viewer_user.account_id,
        display_name="Their Worker",
        commit=True,
    )
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user

    with TestClient(app) as viewer_client:
        response = viewer_client.post(
            NOTES_URL, json={"agent_id": str(viewer_agent.id), "text": "do this"}
        )

    assert response.status_code == 403
    assert "control_managed_agent" in response.json()["detail"]
    assert (
        crud_agent_control_command.list_notes(
            db_session,
            account_id=test_viewer_user.account_id,
            managed_agent_id=viewer_agent.id,
        )
        == []
    )


def test_listing_requires_the_agent_control_permission(db_session, test_viewer_user):
    """A viewer cannot read notes on OSS either, where the decorator is a no-op."""
    viewer_agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_viewer_user.account_id,
        display_name="Their Worker",
        commit=True,
    )
    crud_agent_control_command.create_note(
        db_session,
        account_id=test_viewer_user.account_id,
        managed_agent_id=viewer_agent.id,
        runtime_session_id=None,
        note_id=operator_notes.new_note_id(),
        body="Do not leak this.",
        envelope={"kind": "message"},
        author_display="Owner",
        author_auth_method="jwt",
        created_by_user_id=None,
        expires_at=None,
    )
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user

    with TestClient(app) as viewer_client:
        response = viewer_client.get(
            NOTES_URL, params={"agent_id": str(viewer_agent.id)}
        )

    assert response.status_code == 403
    assert "control_managed_agent" in response.json()["detail"]


# --- the hook pull ----------------------------------------------------------


def _issue_runtime_token(client) -> str:
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


def _runtime_agent(db_session, test_user) -> ManagedAgent:
    return (
        db_session.query(ManagedAgent)
        .filter(ManagedAgent.account_id == test_user.account_id)
        .order_by(ManagedAgent.created_at.desc())
        .first()
    )


def test_hook_pull_delivers_pending_notes_once(client, db_session, test_user):
    """A harness hook gets the block, the store records the hook channel."""
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    note_id = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Run the linter first."}
    ).json()["note_id"]

    response = client.post(
        PENDING_URL,
        json={"channel": "hook"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [entry["note_id"] for entry in body["notes"]] == [note_id]
    assert "Run the linter first." in body["text"]
    assert f'<operator-note id="{note_id}"' in body["text"]
    # The same block, shaped for a Claude Code channel server to forward.
    assert body["channel_event"]["method"] == "notifications/claude/channel"
    assert body["channel_event"]["params"]["source"] == "preloop-operator-notes"

    again = client.post(
        PENDING_URL,
        json={"channel": "hook"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert again.json()["notes"] == []
    assert again.json()["text"] is None

    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    assert stored.status == "delivered"
    assert stored.delivery_channel == operator_notes.CHANNEL_HOOK


def test_hook_pull_requires_a_runtime_bearer(client):
    assert client.post(PENDING_URL, json={"channel": "hook"}).status_code == 401


def test_hook_pull_rejects_the_gateway_channel(client):
    """Gateway delivery is stamped by the gateway, never by a harness pull."""
    token = _issue_runtime_token(client)
    response = client.post(
        PENDING_URL,
        json={"channel": "gateway"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_permission_check_carries_a_pending_note(client, db_session, test_user):
    """The existing hook endpoint returns the note as additional context."""
    from unittest.mock import AsyncMock, patch

    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    note_id = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Do not touch main."}
    ).json()["note_id"]
    decide = AsyncMock(return_value=("allow", "Approved via Preloop.", "req-1", False))

    with patch(
        "preloop.api.endpoints.agent_permission.request_agent_permission", decide
    ):
        response = client.post(
            PERMISSION_CHECK_URL,
            json={"source": "claude_code", "tool_name": "Bash", "tool_input": {}},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    note_text = response.json()["operator_note"]
    assert note_text is not None
    assert f'<operator-note id="{note_id}"' in note_text
    assert "Do not touch main." in note_text

    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    assert stored.delivery_channel == operator_notes.CHANNEL_HOOK


def test_expired_notes_are_not_handed_to_a_hook(client, db_session, test_user):
    """Expiry is enforced on the pull path too, not only in the gateway."""
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    note_id = client.post(
        NOTES_URL,
        json={"agent_id": str(agent.id), "text": "stale", "expires_in_seconds": 60},
    ).json()["note_id"]
    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    response = client.post(
        PENDING_URL,
        json={"channel": "hook"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.json()["notes"] == []


# --- lifecycle events and session end (#1551) -------------------------------


def test_hook_pull_records_the_lifecycle_event(client, db_session, test_user):
    """The Stop pull stamps the event, so a rescue is distinguishable."""
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    note_id = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Ship the fix."}
    ).json()["note_id"]

    response = client.post(
        PENDING_URL,
        json={"channel": "hook", "event": "stop"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    assert stored.status == "delivered"
    assert stored.delivery_event == "stop"


def test_hook_pull_rejects_an_unknown_lifecycle_event(client):
    token = _issue_runtime_token(client)
    response = client.post(
        PENDING_URL,
        json={"channel": "hook", "event": "whenever"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_session_end_marks_session_only_notes_undeliverable(
    client, db_session, test_user
):
    """A note whose only session ended fails; an agent note keeps waiting."""
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    session_note_id = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Too late."}
    ).json()["note_id"]
    # A note addressed to the agent with no session waits for the next run and
    # must survive this session ending.
    agent_note_id = operator_notes.new_note_id()
    crud_agent_control_command.create_note(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=None,
        note_id=agent_note_id,
        body="Wait for the next run.",
        envelope={"kind": "message"},
        author_display="Test User",
        author_auth_method="jwt",
        created_by_user_id=None,
        expires_at=None,
    )

    response = client.post(
        SESSION_END_URL,
        json={"external_session_id": "laptop", "reason": "exit"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["marked"] == 1
    assert body["runtime_session_id"] == str(agent.runtime_session_id)
    ended = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=session_note_id
    )
    assert ended.status == "undeliverable"
    assert operator_notes.note_state(ended) == "undeliverable"
    still_waiting = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=agent_note_id
    )
    assert still_waiting.status == "pending"


def test_session_end_requires_a_runtime_bearer(client):
    assert client.post(SESSION_END_URL, json={"reason": "exit"}).status_code == 401


SESSION_START_URL = "/api/v1/agents/session-start"


def _start_run(client, token: str, external_id: str) -> str:
    response = client.post(
        SESSION_START_URL,
        json={"session_id": external_id, "source": "claude_code"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["runtime_session_id"]


def test_session_start_pull_claims_notes_for_the_registered_run(
    client, db_session, test_user
):
    """A note sent to a `claude -p` run before its first tool call lands at
    SessionStart, even when the token is bound to the agent's identity
    session rather than the run."""
    token = _issue_runtime_token(client)
    run_id = _start_run(client, token, "run-early")
    note_id = client.post(
        NOTES_URL, json={"runtime_session_id": run_id, "text": "Use eu-west-1."}
    ).json()["note_id"]

    response = client.post(
        PENDING_URL,
        json={"channel": "hook", "event": "session_start", "session_id": "run-early"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert [n["note_id"] for n in response.json()["notes"]] == [note_id]
    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    assert stored.status == "delivered"
    assert stored.delivery_event == "session_start"
    assert str(stored.runtime_session_id) == run_id


def test_session_end_sweeps_the_run_named_by_the_harness(client, db_session, test_user):
    """SessionEnd fails the run's notes in one request, keyed by the harness
    session id the hook knows."""
    token = _issue_runtime_token(client)
    run_id = _start_run(client, token, "run-late")
    note_id = client.post(
        NOTES_URL, json={"runtime_session_id": run_id, "text": "Too late."}
    ).json()["note_id"]

    response = client.post(
        SESSION_END_URL,
        json={"external_session_id": "run-late", "reason": "other"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"runtime_session_id": run_id, "marked": 1}
    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    assert stored.status == "undeliverable"


def test_session_end_cannot_reach_another_principals_session(
    client, db_session, test_user
):
    """The body names a harness id, never a session the caller does not own:
    a same-account session keyed on the bare id (an importer row with no
    runtime principal) is not ended and its notes stay pending."""
    from preloop.models.crud import crud_runtime_session

    token = _issue_runtime_token(client)
    foreign = crud_runtime_session.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "session_source_type": "claude_code",
            "session_source_id": "someone-elses-run",
            "started_at": datetime.now(UTC),
        },
    )
    note_id = operator_notes.new_note_id()
    crud_agent_control_command.create_note(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=_runtime_agent(db_session, test_user).id,
        runtime_session_id=foreign.id,
        note_id=note_id,
        body="Not yours to fail.",
        envelope={"kind": "message"},
        author_display="Test User",
        author_auth_method="jwt",
        created_by_user_id=None,
        expires_at=None,
    )

    response = client.post(
        SESSION_END_URL,
        json={"external_session_id": "someone-elses-run", "reason": "exit"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Nothing the caller owns matched, so nothing is ended: not the foreign
    # session and not the token's own identity session either.
    assert response.status_code == 200, response.text
    assert response.json() == {"runtime_session_id": None, "marked": 0}
    db_session.expire_all()
    assert crud_runtime_session.get(db_session, id=foreign.id).ended_at is None
    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    assert stored.status == "pending"


def test_operator_ending_a_session_fails_its_session_only_notes(
    client, db_session, test_user
):
    """Ending a run from the console is a session end too: notes addressed
    only to it move to undeliverable in the same request."""
    token = _issue_runtime_token(client)
    run_id = _start_run(client, token, "run-console-end")
    note_id = client.post(
        NOTES_URL, json={"runtime_session_id": run_id, "text": "Never read."}
    ).json()["note_id"]

    response = client.patch(
        f"/api/v1/runtime-sessions/{run_id}", json={"action": "end"}
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    stored = crud_agent_control_command.get_note(
        db_session, account_id=test_user.account_id, note_id=note_id
    )
    assert stored.status == "undeliverable"
