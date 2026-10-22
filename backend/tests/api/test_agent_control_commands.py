"""A command never targets a terminal session the plugin cannot drive."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from preloop.models.crud import (
    crud_agent_control_command,
    crud_managed_agent,
    crud_runtime_session,
)

from tests.endpoints.test_agent_control_persistence import _setup_controllable_agent


def _live_terminal_agent(client, db_session, test_user, source_id):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id=source_id
    )
    agent.control_last_heartbeat_at = datetime.now(UTC)
    agent.control_reachable_session_ids = ["sdk-owned-1"]
    db_session.add(agent)
    now = datetime.now(UTC)
    terminal = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=f"tui-{uuid.uuid4()}",
        runtime_principal_type=agent.session_source_type,
        runtime_principal_id=agent.session_source_id,
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()
    return agent, terminal


def _nats(mock_get_nats_client):
    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_existing_terminal_session_command_refused_409(
    mock_get_nats_client, client, db_session, test_user
):
    _nats(mock_get_nats_client)
    agent, terminal = _live_terminal_agent(
        client, db_session, test_user, "cmd-terminal-409"
    )

    response = client.post(
        f"/api/v1/agents/{agent.id}/control/commands",
        json={"message": "hello", "target_session_id": str(terminal.id)},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "terminal_session"
    assert (
        crud_agent_control_command.list_recent_for_agent(
            db_session, account_id=test_user.account_id, managed_agent_id=agent.id
        )
        == []
    )


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_new_session_command_allowed_with_terminal_sessions_live(
    mock_get_nats_client, client, db_session, test_user
):
    _nats(mock_get_nats_client)
    agent, _terminal = _live_terminal_agent(
        client, db_session, test_user, "cmd-terminal-new"
    )

    response = client.post(
        f"/api/v1/agents/{agent.id}/control/prompts",
        json={"message": "start fresh", "start_new_session": True},
    )

    assert response.status_code == 202, response.text
    assert response.json()["session_mode"] == "new"


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_new_session_follow_up_command_is_accepted(
    mock_get_nats_client, client, db_session, test_user
):
    """The tracking row a new session returns takes the next line as a command."""
    _nats(mock_get_nats_client)
    agent, _terminal = _live_terminal_agent(
        client, db_session, test_user, "cmd-terminal-follow-up"
    )
    first = client.post(
        f"/api/v1/agents/{agent.id}/control/prompts",
        json={"message": "start fresh", "start_new_session": True},
    )
    assert first.status_code == 202, first.text
    created = first.json()["target_session_id"]

    control = client.get(f"/api/v1/runtime-sessions/{created}/control").json()
    assert (control["mode"], control["delivery"]) == ("command", "turn")
    second = client.post(
        f"/api/v1/agents/{agent.id}/control/prompts",
        json={"message": "and continue", "target_session_id": created},
    )
    assert second.status_code == 202, second.text


def test_presence_frame_reachable_ids_are_stored(client, db_session, test_user):
    """A heartbeat's reachable_session_ids reach the agent row; absence keeps it."""
    agent, token_body = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="cmd-presence-ingest"
    )
    agent_id = str(agent.id)

    def stored():
        db_session.expire_all()
        row = crud_managed_agent.get_for_account(
            db_session, account_id=str(test_user.account_id), agent_id=agent_id
        )
        return row.control_reachable_session_ids

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        assert websocket.receive_json()["name"] == "connected"
        websocket.send_json(
            {
                "type": "heartbeat",
                "message_id": "hb-reach",
                "payload": {"reachable_session_ids": ["sdk-a", "sdk-b"]},
            }
        )
        assert websocket.receive_json()["name"] == "heartbeat"
        assert stored() == ["sdk-a", "sdk-b"]

        websocket.send_json(
            {"type": "heartbeat", "message_id": "hb-plain", "payload": {}}
        )
        assert websocket.receive_json()["name"] == "heartbeat"
        assert stored() == ["sdk-a", "sdk-b"]
