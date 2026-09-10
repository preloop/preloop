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
