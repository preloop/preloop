"""Presence heartbeat rules at the CRUD level.

``clear_control_heartbeat`` is what retires the Agent Control badge when a
socket closes. Its ``not_newer_than`` guard exists for the race where the
plugin has already reconnected (possibly to another api replica) by the time
a closing socket gets around to its teardown: the newer heartbeat must
survive. The endpoint tests cannot reach that comparison, because an evicted
connection never gets as far as calling this, so it is pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models import models
from preloop.models.crud import crud_managed_agent


def _make_agent(db_session, test_user, *, source_id: str) -> models.ManagedAgent:
    """Insert one active managed agent that holds a control heartbeat."""
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=test_user.account_id,
        runtime_session_id=None,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=source_id,
        display_name=f"claude_code {source_id}",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
        created_at=now,
    )
    db_session.add(agent)
    db_session.flush()
    return agent


def test_clear_control_heartbeat_keeps_a_newer_beat(db_session, test_user):
    """A late teardown must not retire presence the reconnect just wrote."""
    agent = _make_agent(db_session, test_user, source_id="reconnect-race-host")
    t1 = datetime.now(UTC) - timedelta(seconds=30)
    t2 = datetime.now(UTC)
    crud_managed_agent.touch_last_seen_for_principal(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="reconnect-race-host",
        observed_at=t2,
        control_session_mode="local",
        control_heartbeat_at=t2,
    )

    # The old socket closes and offers the beat it last wrote, which is older
    # than what the new socket has since stored.
    crud_managed_agent.clear_control_heartbeat(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="reconnect-race-host",
        not_newer_than=t1,
    )

    db_session.refresh(agent)
    assert agent.control_last_heartbeat_at is not None
    assert agent.control_session_mode == "local"


def test_clear_control_heartbeat_retires_its_own_beat(db_session, test_user):
    """The socket that wrote the stored beat is the one allowed to clear it."""
    agent = _make_agent(db_session, test_user, source_id="clean-close-host")
    t2 = datetime.now(UTC)
    crud_managed_agent.touch_last_seen_for_principal(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="clean-close-host",
        observed_at=t2,
        control_session_mode="local",
        control_heartbeat_at=t2,
    )

    crud_managed_agent.clear_control_heartbeat(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="clean-close-host",
        not_newer_than=t2,
    )

    db_session.refresh(agent)
    assert agent.control_last_heartbeat_at is None
    assert agent.control_session_mode is None
