"""Per-session Talk routing: command for sessions the plugin drives, note else.

A Claude or Codex plugin runs once per machine and is online for every
session there, but it can only drive sessions it owns through its SDK (and
a launcher session it holds locally). Everything else is a plain terminal
session, where a command would fork the user's transcript.
"""

import uuid
from datetime import UTC, datetime

from preloop.models.crud import crud_runtime_session
from preloop.services.agent_control_dispatch import resolve_session_control_mode

from tests.endpoints.test_agent_control_persistence import _setup_controllable_agent


def _online(db_session, agent, reachable):
    agent.control_last_heartbeat_at = datetime.now(UTC)
    agent.control_reachable_session_ids = reachable
    db_session.add(agent)
    db_session.commit()


def _terminal_session(db_session, test_user, agent, native_id):
    now = datetime.now(UTC)
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=native_id,
        runtime_principal_type=agent.session_source_type,
        runtime_principal_id=agent.session_source_id,
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    return session


def _resolve(db_session, test_user, session):
    return resolve_session_control_mode(
        db_session, account_id=str(test_user.account_id), session=session
    )


def test_terminal_session_resolves_note_mode_when_not_reachable(
    client, db_session, test_user
):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="route-terminal"
    )
    _online(db_session, agent, ["sdk-owned-1"])
    session = _terminal_session(db_session, test_user, agent, f"tui-{uuid.uuid4()}")

    decision = _resolve(db_session, test_user, session)

    assert decision.mode == "note"
    assert decision.reason_code == "terminal_session"
    assert decision.delivery == "note"
    assert "next tool call" in decision.reason
    assert "next prompt in the terminal" in decision.reason


def test_sdk_owned_session_resolves_command_mode(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="route-sdk"
    )
    native = f"sdk-{uuid.uuid4()}"
    _online(db_session, agent, [native])
    session = _terminal_session(db_session, test_user, agent, native)

    decision = _resolve(db_session, test_user, session)

    assert (decision.mode, decision.reason_code) == ("command", None)
    assert decision.delivery == "turn"


def test_launcher_session_resolves_command_mode(client, db_session, test_user):
    """A launcher session in local mode is reported as reachable too."""
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="route-launcher"
    )
    launcher = f"launcher-{uuid.uuid4()}"
    _online(db_session, agent, ["sdk-owned-1", launcher])
    session = _terminal_session(db_session, test_user, agent, launcher)

    assert _resolve(db_session, test_user, session).mode == "command"


def test_plugin_without_reachable_list_keeps_command_mode(
    client, db_session, test_user
):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="route-legacy"
    )
    _online(db_session, agent, None)
    session = _terminal_session(db_session, test_user, agent, f"tui-{uuid.uuid4()}")

    assert _resolve(db_session, test_user, session).mode == "command"


def test_agent_own_control_session_is_reachable(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="route-own"
    )
    _online(db_session, agent, [])
    own = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(agent.runtime_session_id),
    )

    assert _resolve(db_session, test_user, own).mode == "command"
