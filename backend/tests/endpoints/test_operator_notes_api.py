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


def test_list_notes_pages_with_a_total(client, agent, runtime_session):
    """The agent page shows the latest few and loads older notes on demand."""
    for text in ("one", "two", "three", "four", "five"):
        client.post(NOTES_URL, json={"agent_id": str(agent.id), "text": text})

    first = client.get(NOTES_URL, params={"agent_id": str(agent.id), "limit": 3})
    older = client.get(
        NOTES_URL, params={"agent_id": str(agent.id), "limit": 3, "offset": 3}
    )

    assert first.status_code == 200, first.text
    assert [n["text"] for n in first.json()["notes"]] == ["five", "four", "three"]
    assert first.json()["total"] == 5
    assert [n["text"] for n in older.json()["notes"]] == ["two", "one"]
    assert older.json()["total"] == 5


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
    assert stored.delivery_event == operator_notes.EVENT_PRE_TOOL_USE


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
        NOTES_URL,
        json={"runtime_session_id": str(agent.runtime_session_id), "text": "Too late."},
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
    # The board's session-end timestamp is recorded by this route too (#1889).
    from preloop.models.models.runtime_session_activity import (
        RuntimeSessionActivity,
    )

    ends = (
        db_session.query(RuntimeSessionActivity)
        .filter(
            RuntimeSessionActivity.runtime_session_id == agent.runtime_session_id,
            RuntimeSessionActivity.activity_type == "session_end",
        )
        .all()
    )
    assert len(ends) == 1 and ends[0].summary is None


def test_session_end_requires_a_runtime_bearer(client):
    assert client.post(SESSION_END_URL, json={"reason": "exit"}).status_code == 401


# Delivery receipt acceptance (#1550).
def test_receipt_read_includes_nullable_transition_timestamps(client, agent):
    created = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Check receipts."}
    ).json()
    response = client.get(f"{NOTES_URL}/{created['note_id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "pending"
    assert body["created_at"]
    for field in (
        "claimed_at",
        "expired_at",
        "undeliverable_at",
        "undeliverable_reason",
    ):
        assert body[field] is None


def test_receipt_read_hides_foreign_notes(client, db_session, agent):
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Receipt foreign account"}
    )
    foreign_agent = crud_managed_agent.create_custom_agent(
        db_session, account_id=foreign.id, display_name="Foreign", commit=True
    )
    note = crud_agent_control_command.create_note(
        db_session,
        account_id=foreign.id,
        managed_agent_id=foreign_agent.id,
        runtime_session_id=None,
        note_id="foreign-receipt",
        body="private",
        envelope={},
        author_display="Author",
        author_auth_method="jwt",
        created_by_user_id=None,
        expires_at=None,
    )
    assert client.get(f"{NOTES_URL}/{note.command_id}").status_code == 404
    own = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Own."}
    ).json()
    assert client.get(f"{NOTES_URL}/{own['note_id']}").status_code == 200


def test_receipt_deferred_claim_ack_is_idempotent_and_scoped(
    client, db_session, test_user
):
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    created = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Carry me later."}
    ).json()
    headers = {"Authorization": f"Bearer {token}"}
    claimed = client.post(
        PENDING_URL, json={"channel": "hook", "defer_delivery": True}, headers=headers
    )
    assert claimed.status_code == 200, claimed.text
    receipt = client.get(f"{NOTES_URL}/{created['note_id']}").json()
    assert receipt["state"] == "claimed"
    assert receipt["claimed_at"] and receipt["delivered_at"] is None
    ack_url = "/api/v1/agents/notes/ack"
    ack = client.post(
        ack_url,
        json={"note_ids": [created["note_id"], "unknown"], "session_id": "laptop"},
        headers=headers,
    )
    assert ack.status_code == 200, ack.text
    delivered = client.get(f"{NOTES_URL}/{created['note_id']}").json()
    assert delivered["state"] == "delivered" and delivered["delivered_at"]
    again = client.post(
        ack_url, json={"note_ids": [created["note_id"]]}, headers=headers
    )
    assert again.status_code == 200
    assert (
        client.get(f"{NOTES_URL}/{created['note_id']}").json()["delivered_at"]
        == delivered["delivered_at"]
    )
    unclaimed = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Still pending."}
    ).json()
    assert (
        client.post(
            ack_url, json={"note_ids": [unclaimed["note_id"]]}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(f"{NOTES_URL}/{unclaimed['note_id']}").json()["state"] == "pending"
    )


def test_receipt_claimed_session_end_is_undeliverable(client, db_session, test_user):
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    created = client.post(
        NOTES_URL,
        json={"runtime_session_id": str(agent.runtime_session_id), "text": "Too late."},
    ).json()
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        PENDING_URL, json={"channel": "hook", "defer_delivery": True}, headers=headers
    )
    response = client.post(SESSION_END_URL, json={"reason": "exit"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["marked"] == 1
    receipt = client.get(f"{NOTES_URL}/{created['note_id']}").json()
    assert receipt["state"] == "undeliverable"
    assert (
        receipt["undeliverable_at"]
        and receipt["undeliverable_reason"] == "session_ended"
    )
    assert client.post(SESSION_END_URL, json={}, headers=headers).json()["marked"] == 0


def test_receipt_state_filter_and_expiry_timestamp(client, db_session, agent):
    created = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Expire."}
    ).json()
    note = crud_agent_control_command.get_note(
        db_session, account_id=agent.account_id, note_id=created["note_id"]
    )
    note.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()
    expired = client.get(
        NOTES_URL, params={"agent_id": str(agent.id), "state": "expired"}
    ).json()["notes"]
    assert [row["note_id"] for row in expired] == [created["note_id"]]
    assert expired[0]["expired_at"]
    assert (
        client.get(
            NOTES_URL, params={"agent_id": str(agent.id), "state": "pending"}
        ).json()["notes"]
        == []
    )


def test_receipt_ack_ignores_another_agents_claim(client, db_session, test_user):
    first_token = _issue_runtime_token(client)
    second_token = client.post(
        TOKEN_URL,
        json={
            "session_source_type": "claude_code",
            "session_source_id": "other-laptop",
            "session_reference": "/other",
            "runtime_principal_name": "Other worker",
        },
    ).json()["token"]
    other = (
        db_session.query(ManagedAgent)
        .filter(
            ManagedAgent.account_id == test_user.account_id,
            ManagedAgent.session_source_id == "other-laptop",
        )
        .one()
    )
    created = client.post(
        NOTES_URL, json={"agent_id": str(other.id), "text": "Private claim."}
    ).json()
    second_headers = {"Authorization": f"Bearer {second_token}"}
    assert (
        client.post(
            PENDING_URL,
            json={"channel": "hook", "defer_delivery": True},
            headers=second_headers,
        ).status_code
        == 200
    )
    ack = client.post(
        "/api/v1/agents/notes/ack",
        json={"note_ids": [created["note_id"]], "session_id": "other-laptop"},
        headers={"Authorization": f"Bearer {first_token}"},
    )
    assert ack.status_code == 200 and ack.json() == {"ok": True}
    assert client.get(f"{NOTES_URL}/{created['note_id']}").json()["state"] == "claimed"


def test_receipt_undeliverable_webhook_emitted_once(client, db_session, test_user):
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    secret = generate_secret()
    db_session.add(
        WebhookEndpoint(
            account_id=test_user.account_id,
            url="https://example.com/receipt",
            secret_encrypted=encrypt_value(secret),
            secret_hint=secret_hint(secret),
            event_types=["agent.note_undeliverable"],
            active=True,
            source=SOURCE_ACCOUNT,
        )
    )
    db_session.flush()
    created = client.post(
        NOTES_URL,
        json={
            "runtime_session_id": str(agent.runtime_session_id),
            "text": "Never carried.",
        },
    ).json()
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(2):
        assert client.post(SESSION_END_URL, json={}, headers=headers).status_code == 200
    rows = (
        db_session.query(WebhookDelivery)
        .filter(
            WebhookDelivery.account_id == test_user.account_id,
            WebhookDelivery.event_type == "agent.note_undeliverable",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["data"]["note_id"] == created["note_id"]
    assert rows[0].payload["data"]["undeliverable_reason"] == "session_ended"


def test_receipt_permission_request_defers_claim(client, db_session, test_user):
    from unittest.mock import AsyncMock, patch

    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    created = client.post(
        NOTES_URL,
        json={"agent_id": str(agent.id), "text": "Read on next carrying hook."},
    ).json()
    with patch(
        "preloop.api.endpoints.agent_permission.request_agent_permission",
        AsyncMock(return_value=("allow", "Allowed", None, False)),
    ):
        response = client.post(
            PERMISSION_CHECK_URL,
            json={
                "source": "codex_cli",
                "tool_name": "Bash",
                "evaluation_phase": "permission_request",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    assert created["note_id"] in response.json()["operator_note"]
    receipt = client.get(f"{NOTES_URL}/{created['note_id']}").json()
    assert receipt["state"] == "claimed" and receipt["delivered_at"] is None


def test_receipt_spool_dropped_records_reason(client, db_session, test_user):
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    created = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Dropped spool."}
    ).json()
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        PENDING_URL, json={"channel": "hook", "defer_delivery": True}, headers=headers
    )
    response = client.post(
        "/api/v1/agents/notes/ack",
        json={
            "note_ids": [created["note_id"]],
            "undeliverable_reason": "spool_dropped",
        },
        headers=headers,
    )
    assert response.status_code == 200
    receipt = client.get(f"{NOTES_URL}/{created['note_id']}").json()
    assert receipt["state"] == "undeliverable"
    assert receipt["undeliverable_reason"] == "spool_dropped"
    assert receipt["undeliverable_at"] and receipt["delivered_at"] is None


@pytest.mark.parametrize(
    "source,phase,event,decision,expected",
    [
        ("codex_cli", "pre_tool_use", "PreToolUse", "allow", "delivered"),
        ("copilot_cli", "permission_request", None, "allow", "pending"),
        *[
            (source, "permission_request", None, "allow", "pending")
            for source in (
                "opencode",
                "pi",
                "deepseek",
                "hermes",
                "openclaw",
                "nanobot",
                "devin_cli",
            )
        ],
        ("cursor", "permission_request", "beforeShellExecution", "allow", "claimed"),
        ("cursor", "pre_tool_use", "preToolUse", "allow", "delivered"),
        ("cursor", "pre_tool_use", "preToolUse", "ask", "claimed"),
    ],
)
def test_receipt_permission_carrier_controls_delivery(
    client, db_session, test_user, source, phase, event, decision, expected
):
    from unittest.mock import AsyncMock, patch

    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    created = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Carrier receipt."}
    ).json()
    with patch(
        "preloop.api.endpoints.agent_permission.request_agent_permission",
        AsyncMock(return_value=(decision, "Decision", None, False)),
    ):
        response = client.post(
            PERMISSION_CHECK_URL,
            json={
                "source": source,
                "tool_name": "Bash",
                "evaluation_phase": phase,
                "hook_event_name": event,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    receipt = client.get(f"{NOTES_URL}/{created['note_id']}").json()
    assert receipt["state"] == expected
    assert bool(receipt["claimed_at"]) == (expected != "pending")
    if expected == "delivered":
        assert receipt["claimed_at"] == receipt["delivered_at"]
    else:
        assert receipt["delivered_at"] is None


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


def test_receipt_durable_hook_ack_uses_its_agents_current_session(
    client, db_session, test_user
):
    from unittest.mock import AsyncMock, patch
    from preloop.models.crud import crud_api_key

    _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    _, token = crud_api_key.create_runtime_key(
        db_session,
        name="durable-receipt",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"managed_agent_id": str(agent.id)},
    )
    db_session.commit()
    created = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Durable hook receipt."}
    ).json()
    headers = {"Authorization": f"Bearer {token}"}
    with patch(
        "preloop.api.endpoints.agent_permission.request_agent_permission",
        AsyncMock(return_value=("allow", "Allowed", None, False)),
    ):
        response = client.post(
            PERMISSION_CHECK_URL,
            json={
                "source": "codex_cli",
                "tool_name": "Bash",
                "evaluation_phase": "permission_request",
            },
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert created["note_id"] in response.json()["operator_note"]
    ack = client.post(
        "/api/v1/agents/notes/ack",
        json={"note_ids": [created["note_id"]]},
        headers=headers,
    )
    assert ack.status_code == 200, ack.text
    assert (
        client.get(f"{NOTES_URL}/{created['note_id']}").json()["state"] == "delivered"
    )


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


def _hosted_runner_session(db_session, test_user) -> tuple:
    """A live Copilot session on the user's runner with a known harness id."""
    from preloop.models import models
    from preloop.services import runner_sessions

    runner = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=test_user.id,
        name="Jane laptop",
        hostname="jane-laptop.example.com",
        token_hash=f"hash-{uuid4().hex}",
        status="online",
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(runner)
    db_session.flush()
    row = runner_sessions.request_session(
        db_session,
        runner=runner,
        actor=test_user,
        harness="copilot_cli",
        model="auto",
        workspace={"kind": "authorized_directory", "id": "dir_1"},
        workspace_label="ims",
    )
    harness_id = str(uuid4())
    for state in ("starting", "idle"):
        runner_sessions.apply_runner_session_message(
            db_session,
            runner,
            {
                "type": "session_state",
                "remote_session_id": str(row.id),
                "state": state,
                "harness_session_id": harness_id,
            },
        )
    db_session.refresh(row)
    return row, harness_id


def test_hook_pull_and_session_end_for_a_hosted_runner_session(
    client, db_session, test_user
):
    """A runner-hosted harness id reaches the runner session's notes (#1482).

    Copilot's SessionEnd hook fires after every per-turn process, so it must
    leave the runner session and its pending notes alone.
    """
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    row, harness_id = _hosted_runner_session(db_session, test_user)
    queued = []
    for body in ("First note.", "Second note."):
        note_id = operator_notes.new_note_id()
        crud_agent_control_command.create_note(
            db_session,
            account_id=test_user.account_id,
            managed_agent_id=agent.id,
            runtime_session_id=row.runtime_session_id,
            note_id=note_id,
            body=body,
            envelope={"kind": "message"},
            author_display="Test User",
            author_auth_method="jwt",
            created_by_user_id=None,
            expires_at=None,
        )
        queued.append(note_id)

    ended = client.post(
        SESSION_END_URL,
        json={"external_session_id": harness_id, "reason": "exit"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert ended.status_code == 200, ended.text
    assert ended.json() == {
        "runtime_session_id": str(row.runtime_session_id),
        "marked": 0,
    }
    runtime = db_session.get(RuntimeSession, row.runtime_session_id)
    db_session.refresh(runtime)
    assert runtime.ended_at is None

    pulled = client.post(
        PENDING_URL,
        json={"channel": "hook", "session_id": harness_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert pulled.status_code == 200, pulled.text
    assert sorted(n["note_id"] for n in pulled.json()["notes"]) == sorted(queued)


@pytest.mark.parametrize("register_first", [False, True])
def test_agent_note_receipt_tracks_native_run_with_enrollment_token(
    client, db_session, test_user, register_first: bool
) -> None:
    """An agent note must ACK against the actual run, not its identity stub."""
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    if register_first:
        run_id = _start_run(client, token, "native-receipt-run")
    created = client.post(
        NOTES_URL,
        json={"agent_id": str(agent.id), "text": "Run the verification."},
    ).json()
    note_id = created["note_id"]
    assert created["runtime_session_id"] is None
    if not register_first:
        run_id = _start_run(client, token, "native-receipt-run")
    headers = {"Authorization": f"Bearer {token}"}
    pulled = client.post(
        PENDING_URL,
        json={"session_id": "native-receipt-run", "defer_delivery": True},
        headers=headers,
    )
    assert pulled.status_code == 200, pulled.text
    assert [note["note_id"] for note in pulled.json()["notes"]] == [note_id]
    acknowledged = client.post(
        "/api/v1/agents/notes/ack",
        json={"session_id": "native-receipt-run", "note_ids": [note_id]},
        headers=headers,
    )
    assert acknowledged.status_code == 200, acknowledged.text
    receipt = client.get(f"{NOTES_URL}/{note_id}").json()
    assert receipt["state"] == "delivered"
    assert receipt["runtime_session_id"] == run_id
    ended = client.post(
        SESSION_END_URL,
        json={"external_session_id": "native-receipt-run"},
        headers=headers,
    )
    assert ended.status_code == 200, ended.text
    assert client.get(f"{NOTES_URL}/{note_id}").json()["state"] == "delivered"


def test_explicit_enrollment_session_note_cannot_leak_into_native_run(
    client, db_session, test_user
) -> None:
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    note_id = client.post(
        NOTES_URL,
        json={
            "runtime_session_id": str(agent.runtime_session_id),
            "text": "Only the named session.",
        },
    ).json()["note_id"]
    _start_run(client, token, "other-native-run")
    headers = {"Authorization": f"Bearer {token}"}
    pulled = client.post(
        PENDING_URL,
        json={"session_id": "other-native-run", "defer_delivery": True},
        headers=headers,
    )
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["notes"] == []
    client.post(
        "/api/v1/agents/notes/ack",
        json={"session_id": "other-native-run", "note_ids": [note_id]},
        headers=headers,
    )
    assert client.get(f"{NOTES_URL}/{note_id}").json()["state"] == "pending"


def test_enrollment_agent_note_waits_for_next_run_after_native_end(
    client, db_session, test_user
) -> None:
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    _start_run(client, token, "ending-native-run")
    note_id = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Brief the next run."}
    ).json()["note_id"]
    headers = {"Authorization": f"Bearer {token}"}
    ended = client.post(
        SESSION_END_URL,
        json={"external_session_id": "ending-native-run"},
        headers=headers,
    )
    assert ended.status_code == 200, ended.text
    assert client.get(f"{NOTES_URL}/{note_id}").json()["state"] == "pending"
    next_run_id = _start_run(client, token, "next-native-run")
    pulled = client.post(
        PENDING_URL,
        json={"session_id": "next-native-run", "defer_delivery": True},
        headers=headers,
    )
    assert [note["note_id"] for note in pulled.json()["notes"]] == [note_id]
    client.post(
        "/api/v1/agents/notes/ack",
        json={"session_id": "next-native-run", "note_ids": [note_id]},
        headers=headers,
    )
    receipt = client.get(f"{NOTES_URL}/{note_id}").json()
    assert receipt["state"] == "delivered"
    assert receipt["runtime_session_id"] == next_run_id


@pytest.mark.parametrize("channel", ["hook", "gateway"])
def test_explicit_next_session_excludes_existing_runs(
    client, db_session, test_user, agent, runtime_session, channel: str
) -> None:
    """Next-session advice waits even while the current run is carrying notes."""
    created = client.post(
        NOTES_URL,
        json={
            "agent_id": str(agent.id),
            "next_session": True,
            "text": "Next run only.",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["next_session"] is True
    assert body["runtime_session_id"] is None
    assert (
        operator_notes.claim_pending_notes(
            db_session,
            account_id=str(test_user.account_id),
            managed_agent_id=str(agent.id),
            runtime_session_id=str(runtime_session.id),
            channel=channel,
        )
        == []
    )
    ordinary = client.post(
        NOTES_URL, json={"agent_id": str(agent.id), "text": "Current run advice."}
    ).json()
    available = operator_notes.claim_pending_notes(
        db_session,
        account_id=str(test_user.account_id),
        managed_agent_id=str(agent.id),
        runtime_session_id=str(runtime_session.id),
        channel=channel,
        limit=1,
    )
    assert [note.command_id for note in available] == [ordinary["note_id"]]
    next_run = RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="next-run",
        started_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    db_session.add(next_run)
    db_session.flush()
    delivered = operator_notes.claim_pending_notes(
        db_session,
        account_id=str(test_user.account_id),
        managed_agent_id=str(agent.id),
        runtime_session_id=str(next_run.id),
        channel=channel,
        turn_index=3,
    )
    assert [note.command_id for note in delivered] == [body["note_id"]]
    db_session.commit()
    receipt = client.get(f"{NOTES_URL}/{body['note_id']}").json()
    assert receipt["runtime_session_id"] == str(next_run.id)
    assert receipt["state"] == "delivered"
    assert receipt["next_session"] is True


@pytest.mark.parametrize("identity", ["exact", "per_run", "managed_agent"])
def test_session_note_is_in_its_agents_history_and_filter(
    client, db_session, test_user, agent, runtime_session, identity: str
) -> None:
    """A native run's durable principal preserves its agent history association."""
    native = RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="native-history-run",
        runtime_principal_type=(
            "managed_agent"
            if identity == "managed_agent"
            else agent.session_source_type
        ),
        runtime_principal_id=(
            str(agent.id)
            if identity == "managed_agent"
            else agent.session_source_id
            + (":native-history-run" if identity == "per_run" else "")
        ),
        started_at=datetime.now(UTC),
    )
    db_session.add(native)
    db_session.flush()
    sent = client.post(
        NOTES_URL, json={"runtime_session_id": str(native.id), "text": "Native note."}
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["managed_agent_id"] == str(agent.id)
    client.post(NOTES_URL, json={"agent_id": str(agent.id), "text": "Other session."})
    page = client.get(
        NOTES_URL,
        params={
            "agent_id": str(agent.id),
            "runtime_session_id": str(native.id),
            "limit": 1,
        },
    ).json()
    assert page["total"] == 1
    assert [note["note_id"] for note in page["notes"]] == [sent.json()["note_id"]]
    assert (
        len(client.get(NOTES_URL, params={"agent_id": str(agent.id)}).json()["notes"])
        == 2
    )


@pytest.mark.parametrize("target", ["runtime_session_id", "execution_id", "alias"])
def test_next_session_requires_an_agent(client, target: str) -> None:
    """Next-session is an agent addressing mode, never an ignored modifier."""
    value = "worker" if target == "alias" else str(uuid4())
    response = client.post(
        NOTES_URL, json={target: value, "next_session": True, "text": "hi"}
    )
    assert response.status_code == 422


def test_hook_note_enters_session_activity_only_after_delivery_ack(
    client, db_session, test_user
) -> None:
    """The transcript records delivery once, with its author and boundary."""
    token = _issue_runtime_token(client)
    run_id = _start_run(client, token, "inline-note-run")
    created = client.post(
        NOTES_URL,
        json={"runtime_session_id": run_id, "text": "Verify before continuing."},
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    headers = {"Authorization": f"Bearer {token}"}
    pulled = client.post(
        PENDING_URL,
        json={
            "session_id": "inline-note-run",
            "defer_delivery": True,
            "event": "pre_tool_use",
        },
        headers=headers,
    )
    assert pulled.status_code == 200, pulled.text
    assert [note["note_id"] for note in pulled.json()["notes"]] == [note_id]
    activity_url = f"/api/v1/runtime-sessions/{run_id}/activity"
    before = client.get(activity_url)
    assert before.status_code == 200, before.text
    assert not [
        item
        for item in before.json()["items"]
        if item["metadata"].get("note_id") == note_id
    ]
    for _ in range(2):
        acknowledged = client.post(
            "/api/v1/agents/notes/ack",
            json={"session_id": "inline-note-run", "note_ids": [note_id]},
            headers=headers,
        )
        assert acknowledged.status_code == 200, acknowledged.text
    activity = client.get(activity_url)
    assert activity.status_code == 200, activity.text
    notes = [
        item
        for item in activity.json()["items"]
        if item["metadata"].get("note_id") == note_id
    ]
    assert len(notes) == 1
    assert notes[0]["activity_type"] == "agent_control_message"
    assert notes[0]["summary"] == "Verify before continuing."
    assert notes[0]["metadata"]["author_display"] == "Test User"
    assert notes[0]["metadata"]["author_auth_method"] == "session"
    assert notes[0]["metadata"]["delivery_event"] == "pre_tool_use"
    receipt = client.get(f"{NOTES_URL}/{note_id}").json()
    assert receipt["author"]["display"] == "Test User"
    assert receipt["state"] == "delivered"
    assert receipt["delivered_at"] is not None
    assert receipt["runtime_session_id"] == run_id


@pytest.mark.parametrize("identity", ["source", "managed_agent"])
def test_session_principal_cannot_attach_a_foreign_agent(
    client, db_session, test_user, identity: str
) -> None:
    """An in-account run never associates a note with another account's agent."""
    foreign_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    foreign = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=foreign_account.id,
        display_name="Other Worker",
        commit=True,
    )
    run = RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="foreign-principal-run",
        runtime_principal_type="managed_agent"
        if identity == "managed_agent"
        else foreign.session_source_type,
        runtime_principal_id=str(foreign.id)
        if identity == "managed_agent"
        else foreign.session_source_id,
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.flush()
    response = client.post(
        NOTES_URL, json={"runtime_session_id": str(run.id), "text": "Own session only."}
    )
    assert response.status_code == 201, response.text
    assert response.json()["runtime_session_id"] == str(run.id)
    assert response.json()["managed_agent_id"] is None
    assert (
        client.post(
            NOTES_URL,
            json={
                "agent_id": str(foreign.id),
                "next_session": True,
                "text": "Foreign.",
            },
        ).status_code
        == 404
    )


def test_note_targets_exclude_identity_and_page_live_conversations(
    client, db_session, test_user
) -> None:
    """An enrollment identity is never the picker's only live session."""
    token = _issue_runtime_token(client)
    agent = _runtime_agent(db_session, test_user)
    target_url = f"{NOTES_URL}/session-targets"
    empty = client.get(target_url, params={"agent_id": str(agent.id)})
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"sessions": [], "total": 0}
    first_id = _start_run(client, token, "target-first")
    second_id = _start_run(client, token, "target-second")
    first_run = db_session.get(RuntimeSession, first_id)
    first_run.alias = "test-worker"
    db_session.commit()
    first = client.get(target_url, params={"agent_id": str(agent.id), "limit": 1})
    older = client.get(
        target_url, params={"agent_id": str(agent.id), "limit": 1, "offset": 1}
    )
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 2
    assert first.json()["sessions"][0]["id"] == second_id
    assert older.json()["sessions"][0]["id"] == first_id
    assert older.json()["sessions"][0]["alias"] == "test-worker"
    assert older.json()["sessions"][0]["session_source_type"] == "claude_code"
    assert older.json()["sessions"][0]["started_at"]
    assert older.json()["sessions"][0]["started_at"].endswith("Z")
    first_run.ended_at = datetime.now(UTC)
    db_session.commit()
    live = client.get(target_url, params={"agent_id": str(agent.id)}).json()
    assert live["total"] == 1
    assert [run["id"] for run in live["sessions"]] == [second_id]


def test_note_targets_unknown_and_foreign_agents_are_404(client, db_session) -> None:
    """Target discovery has the same account boundary as sending."""
    foreign_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    foreign = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=foreign_account.id,
        display_name="Other Worker",
        commit=True,
    )
    for agent_id in (foreign.id, uuid4()):
        response = client.get(
            f"{NOTES_URL}/session-targets", params={"agent_id": str(agent_id)}
        )
        assert response.status_code == 404


def test_focused_note_is_included_beyond_recent_activity_page(
    client, db_session, test_user, agent, runtime_session
) -> None:
    """An older note link can retrieve its inline row without unbounded history."""
    from preloop.models import models

    created = client.post(
        NOTES_URL,
        json={
            "runtime_session_id": str(runtime_session.id),
            "text": "Earlier instruction.",
        },
    ).json()
    operator_notes.claim_pending_notes(
        db_session,
        account_id=str(test_user.account_id),
        managed_agent_id=str(agent.id),
        runtime_session_id=str(runtime_session.id),
        channel="hook",
    )
    now = datetime.now(UTC)
    db_session.add_all(
        [
            models.RuntimeSessionActivity(
                account_id=test_user.account_id,
                runtime_session_id=runtime_session.id,
                activity_type="tool_call",
                summary=f"Later tool {index}",
                status="success",
                timestamp=now + timedelta(seconds=index + 1),
                metadata_={},
            )
            for index in range(100)
        ]
    )
    db_session.commit()
    activity_url = f"/api/v1/runtime-sessions/{runtime_session.id}/activity"
    recent = client.get(activity_url)
    assert recent.status_code == 200, recent.text
    assert not [
        item
        for item in recent.json()["items"]
        if item["metadata"].get("note_id") == created["note_id"]
    ]
    focused = client.get(activity_url, params={"operator_note_id": created["note_id"]})
    assert focused.status_code == 200, focused.text
    notes = [
        item
        for item in focused.json()["items"]
        if item["metadata"].get("note_id") == created["note_id"]
    ]
    assert len(notes) == 1
    assert notes[0]["summary"] == "Earlier instruction."
    assert len(focused.json()["items"]) == len(recent.json()["items"]) + 1
    other_session = RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="unrelated-focus-session",
        started_at=now,
    )
    db_session.add(other_session)
    db_session.commit()
    unrelated = client.get(
        f"/api/v1/runtime-sessions/{other_session.id}/activity",
        params={"operator_note_id": created["note_id"]},
    )
    assert unrelated.status_code == 200, unrelated.text
    assert not [
        item
        for item in unrelated.json()["items"]
        if item["metadata"].get("note_id") == created["note_id"]
    ]
