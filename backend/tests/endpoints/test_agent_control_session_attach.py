"""Terminal attach command mode for managed agents (#1150).

``GET /runtime-sessions/{id}/control`` picks the attach input mode and
``GET /agents/{id}/control/commands/{command_id}`` reports a command's
delivery state as queued, delivered, started, then finished.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from preloop.models.crud import (
    crud_agent_control_command,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.crud.agent_control_command import COMMAND_RESULT_ENVELOPE_KEY
from preloop.services.agent_control_dispatch import command_delivery_state

from tests.endpoints.test_agent_control_persistence import (
    _persist_command,
    _setup_controllable_agent,
)


def _online(db_session, agent):
    agent.control_last_heartbeat_at = datetime.now(UTC)
    db_session.add(agent)
    db_session.commit()


def _hook_session(db_session, test_user, *, principal=None):
    now = datetime.now(UTC)
    kwargs = {}
    if principal is not None:
        kwargs = {
            "runtime_principal_type": principal.session_source_type,
            "runtime_principal_id": principal.session_source_id,
        }
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=f"claude-p-{uuid.uuid4()}",
        started_at=now,
        last_activity_at=now,
        **kwargs,
    )


def _mode(client, session_id):
    response = client.get(f"/api/v1/runtime-sessions/{session_id}/control")
    assert response.status_code == 200, response.text
    return response.json()


def test_online_managed_agent_session_is_command_mode(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-hermes-online"
    )
    _online(db_session, agent)

    body = _mode(client, agent.runtime_session_id)

    assert body["mode"] == "command"
    assert body["reason_code"] is None
    assert body["managed_agent_id"] == str(agent.id)
    assert body["agent_kind"] == "openclaw"


def test_hook_governed_session_stays_note_and_says_why(client, db_session, test_user):
    session = _hook_session(db_session, test_user)

    body = _mode(client, session.id)

    assert body["mode"] == "note"
    assert body["reason_code"] == "not_managed"
    assert "next tool or model call" in body["reason"]
    assert body["managed_agent_id"] is None


def test_offline_agent_is_note_mode(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-offline"
    )
    agent.control_last_heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    body = _mode(client, agent.runtime_session_id)

    assert body["mode"] == "note"
    assert body["reason_code"] == "control_offline"
    assert "no live Agent Control connection" in body["reason"]


def test_agent_without_control_plugin_is_note_mode(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-plugin"
    )
    _online(db_session, agent)
    with patch(
        "preloop.services.agent_control_dispatch.agent_has_control_config",
        return_value=False,
    ):
        body = _mode(client, agent.runtime_session_id)
    assert body["reason_code"] == "no_control_plugin"


def test_ended_session_is_note_mode(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-ended"
    )
    _online(db_session, agent)
    session = crud_runtime_session.get_account_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(agent.runtime_session_id),
    )
    session.ended_at = datetime.now(UTC)
    db_session.commit()

    assert _mode(client, session.id)["reason_code"] == "session_ended"


def test_session_owned_through_principal_resolves_its_agent(
    client, db_session, test_user
):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-principal"
    )
    _online(db_session, agent)
    session = _hook_session(db_session, test_user, principal=agent)

    body = _mode(client, session.id)

    assert body["mode"] == "command"
    assert body["managed_agent_id"] == str(agent.id)


def test_unknown_and_malformed_sessions_are_404(client):
    assert (
        client.get(f"/api/v1/runtime-sessions/{uuid.uuid4()}/control").status_code
        == 404
    )
    assert client.get("/api/v1/runtime-sessions/nope/control").status_code == 404


def test_command_mode_session_accepts_a_targeted_prompt(client, db_session, test_user):
    """What command mode promises: the prompt endpoint takes this session."""
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-target"
    )
    _online(db_session, agent)
    assert _mode(client, agent.runtime_session_id)["mode"] == "command"

    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new=AsyncMock(return_value=True),
    ):
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={
                "message": "run the tests again",
                "target_session_id": str(agent.runtime_session_id),
                "metadata": {"source": "cli_attach"},
            },
        )
    assert response.status_code == 202, response.text
    assert response.json()["session_mode"] == "existing"
    record = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=test_user.account_id,
        command_id=response.json()["command_id"],
    )
    assert record.source == "cli_attach"
    assert record.created_by_user_id == test_user.id
    rows = crud_runtime_session_activity.list_for_runtime_session(
        db_session,
        account_id=str(test_user.account_id),
        runtime_session_id=str(agent.runtime_session_id),
    )
    row = next(r for r in rows if r.activity_type == "agent_control_message")
    assert row.metadata_["kind"] == "operator_command"
    # The stored row names the sender in clear (not masked by the redactor).
    assert row.metadata_["sent_by"] == (
        test_user.full_name or test_user.username or test_user.email
    )


def test_command_status_walks_queued_delivered_started_finished(
    client, db_session, test_user
):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-status"
    )
    record = _persist_command(db_session, test_user, agent)
    path = f"/api/v1/agents/{agent.id}/control/commands/{record.command_id}"

    def state():
        response = client.get(path)
        assert response.status_code == 200, response.text
        return response.json()

    assert state()["delivery_state"] == "queued"
    crud_agent_control_command.mark_delivered(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        command_id=record.command_id,
        delivered_at=datetime.now(UTC),
    )
    assert state()["delivery_state"] == "delivered"
    crud_agent_control_command.mark_acked(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        command_id=record.command_id,
        acked_at=datetime.now(UTC),
    )
    started = state()
    assert started["delivery_state"] == "started"
    assert started["terminal"] is False
    crud_agent_control_command.mark_terminal_result(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        command_id=record.command_id,
        result_payload={"command_id": record.command_id, "status": "completed"},
    )
    finished = state()
    assert finished["delivery_state"] == "finished"
    assert finished["result_status"] == "completed"
    assert finished["terminal"] is True


def test_command_status_is_scoped_to_the_agent(client, db_session, test_user):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-scope-a"
    )
    other, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-scope-b"
    )
    record = _persist_command(db_session, test_user, agent)

    assert (
        client.get(
            f"/api/v1/agents/{other.id}/control/commands/{record.command_id}"
        ).status_code
        == 404
    )
    assert client.get("/api/v1/agents/not-a-uuid/control/commands/x").status_code == 404


class _Row:
    def __init__(self, status, envelope=None):
        self.status = status
        self.envelope = envelope or {}


def test_delivery_state_mapping():
    assert command_delivery_state(_Row("pending")) == ("queued", None)
    assert command_delivery_state(_Row("delivered")) == ("delivered", None)
    assert command_delivery_state(_Row("acked")) == ("started", None)
    assert command_delivery_state(_Row("acked", {COMMAND_RESULT_ENVELOPE_KEY: {}})) == (
        "finished",
        "completed",
    )
    assert command_delivery_state(
        _Row("acked", {COMMAND_RESULT_ENVELOPE_KEY: {"status": "error"}})
    ) == ("failed", "error")
    assert command_delivery_state(_Row("failed")) == ("failed", "failed")
    assert command_delivery_state(_Row("expired")) == ("expired", None)
    assert command_delivery_state(_Row("cancelled")) == ("cancelled", None)


def test_targeted_prompt_is_put_on_the_session_live_stream(
    client, db_session, test_user
):
    """The console and attach see the new turn when it is sent, not later."""
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-live"
    )
    _online(db_session, agent)
    with (
        patch(
            "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
            new=AsyncMock(return_value=True),
        ),
        patch("preloop.api.endpoints.agent_control.emit_account_event") as emitted,
    ):
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={
                "message": "recount zone B",
                "target_session_id": str(agent.runtime_session_id),
            },
        )
    assert response.status_code == 202, response.text
    events = [call.args[0] for call in emitted.call_args_list]
    live = [e for e in events if e["type"] == "runtime_session_updated"]
    assert len(live) == 1
    event = live[0]
    assert event["topic"] == "runtime_sessions"
    assert event["runtime_session_id"] == str(agent.runtime_session_id)
    payload = event["payload"]
    assert payload["activity_type"] == "agent_control_message"
    assert payload["status"] == "delivered"
    assert payload["summary"] == "recount zone B"
    assert payload["metadata"]["kind"] == "operator_command"
    assert payload["metadata"]["command_id"] == response.json()["command_id"]
    assert payload["metadata"]["sent_by"] == (
        test_user.full_name or test_user.username or test_user.email
    )
    # The account-wide agent-control event is still sent as before.
    assert any(e["type"] == "managed_agent_command_sent" for e in events)


def test_untargeted_prompt_without_history_session_emits_no_session_event(
    client, db_session, test_user
):
    agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="attach-nohistory"
    )
    _online(db_session, agent)
    with (
        patch(
            "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "preloop.api.endpoints.agent_control._command_history_session",
            return_value=None,
        ),
        patch("preloop.api.endpoints.agent_control.emit_account_event") as emitted,
    ):
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "hello"},
        )
    assert response.status_code == 202, response.text
    types = [call.args[0]["type"] for call in emitted.call_args_list]
    assert "runtime_session_updated" not in types
