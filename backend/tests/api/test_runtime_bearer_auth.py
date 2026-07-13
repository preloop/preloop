"""Real-database tests for runtime bearer token authentication.

Regression: durable managed-agent credentials are minted WITHOUT a
runtime-session binding, but ``authenticate_runtime_bearer_token`` (the Agent
Control WebSocket auth) hard-required one — so every runtime plugin using a
credential minted after sessions became lazy was rejected with an endless
403 reconnect loop, and the Console/mobile showed the agents as offline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.auth.jwt import authenticate_runtime_bearer_token
from preloop.models.crud import crud_api_key
from preloop.models.models import ManagedAgent, RuntimeSession


def _make_agent(db_session, test_user, *, source_id: str) -> ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind="hermes",
        session_source_type="hermes",
        session_source_id=source_id,
        display_name="Hermes",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def _mint_sessionless_credential(db_session, test_user, agent) -> str:
    _, token = crud_api_key.create_runtime_key(
        db_session,
        name=f"Managed Agent Credential: {agent.display_name}",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "managed_agent_id": str(agent.id),
            "runtime_principal": {
                "type": "hermes",
                "id": agent.session_source_id,
                "name": agent.display_name,
            },
        },
    )
    return token


def test_sessionless_durable_credential_authenticates(db_session, test_user):
    """A durable credential without a session binding must authenticate,
    resolving (creating) the agent's identity session."""
    agent = _make_agent(db_session, test_user, source_id="hermes-authtest-1")
    token = _mint_sessionless_credential(db_session, test_user, agent)

    context = authenticate_runtime_bearer_token(
        db_session, token, enforce_current_binding=False
    )

    assert context.managed_agent.id == agent.id
    assert context.runtime_session is not None
    assert context.runtime_session.ended_at is None
    assert context.runtime_session.session_source_id == "hermes-authtest-1"

    # Reconnecting reuses the same identity session (no session-per-connect).
    context_again = authenticate_runtime_bearer_token(
        db_session, token, enforce_current_binding=False
    )
    assert context_again.runtime_session.id == context.runtime_session.id


def test_sessionless_credential_reopens_ended_session(db_session, test_user):
    """An ended identity session is reopened so control reconnects survive
    operator 'end session' actions."""
    agent = _make_agent(db_session, test_user, source_id="hermes-authtest-2")
    now = datetime.now(UTC).replace(tzinfo=None)
    ended = RuntimeSession(
        id=uuid4(),
        account_id=test_user.account_id,
        session_source_type="hermes",
        session_source_id="hermes-authtest-2",
        runtime_principal_type="hermes",
        runtime_principal_id="hermes-authtest-2",
        started_at=now - timedelta(hours=2),
        last_activity_at=now - timedelta(hours=1),
        ended_at=now - timedelta(minutes=30),
    )
    db_session.add(ended)
    db_session.commit()
    token = _mint_sessionless_credential(db_session, test_user, agent)

    context = authenticate_runtime_bearer_token(
        db_session, token, enforce_current_binding=False
    )

    assert context.runtime_session.id == ended.id
    assert context.runtime_session.ended_at is None, "session must be reopened"


def test_credential_without_agent_binding_is_rejected(db_session, test_user):
    """Tokens lacking a managed-agent binding still fail closed."""
    _, token = crud_api_key.create_runtime_key(
        db_session,
        name="Unbound runtime key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"flow_execution_id": "flow-123"},
    )
    with pytest.raises(HTTPException) as exc_info:
        authenticate_runtime_bearer_token(
            db_session, token, enforce_current_binding=False
        )
    assert exc_info.value.status_code == 401


def test_stale_session_binding_falls_back_to_identity_session(db_session, test_user):
    """A credential bound to a DELETED session recovers instead of 403ing."""
    agent = _make_agent(db_session, test_user, source_id="hermes-authtest-3")
    _, token = crud_api_key.create_runtime_key(
        db_session,
        name="Stale-bound runtime key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={
            "managed_agent_id": str(agent.id),
            "runtime_session_id": str(uuid4()),  # points nowhere
        },
    )
    context = authenticate_runtime_bearer_token(
        db_session, token, enforce_current_binding=False
    )
    assert context.runtime_session is not None
    assert context.runtime_session.session_source_id == "hermes-authtest-3"
