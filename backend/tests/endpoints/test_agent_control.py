"""Endpoint tests for the managed-agent control plane."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect

from preloop.models.crud import (
    crud_agent_control_command,
    crud_managed_agent,
    crud_managed_agent_enrollment,
    crud_runtime_session,
    crud_runtime_session_activity,
)


def _issue_runtime_token(client, *, session_source_id: str = "openclaw-live"):
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "openclaw",
            "session_source_id": session_source_id,
            "session_reference": "/tmp/openclaw.json",
            "runtime_principal_name": "OpenClaw Live Agent",
        },
    )
    assert response.status_code == 201
    return response.json()


def _mark_agent_control_configured(db_session, test_user, managed_agent) -> None:
    crud_managed_agent_enrollment.create_for_agent(
        db_session,
        account_id=test_user.account_id,
        agent_id=managed_agent.id,
        created_by_user_id=test_user.id,
        enrollment_type="cli_managed_config",
        adapter_key="openclaw",
        managed_config={
            "preloop": {
                "control": {
                    "enabled": True,
                    "control_ws_url": (
                        "wss://preloop.example/api/v1/agents/control/ws"
                    ),
                    "adapter_package": "preloop.integrations.agent_control",
                }
            }
        },
        validation_result={
            "control_channel_configured": True,
            "control_ws_url_ok": True,
            "control_bearer_token_ok": True,
        },
    )


def _mark_agent_control_install_pending(db_session, test_user, managed_agent) -> None:
    crud_managed_agent_enrollment.create_for_agent(
        db_session,
        account_id=test_user.account_id,
        agent_id=managed_agent.id,
        created_by_user_id=test_user.id,
        enrollment_type="cli_managed_config",
        adapter_key="openclaw",
        managed_config={
            "preloop": {
                "control": {
                    "enabled": True,
                    "control_ws_url": (
                        "wss://preloop.example/api/v1/agents/control/ws"
                    ),
                }
            }
        },
        validation_result={},
    )


def _mark_runtime_control_verified(db_session, test_user, managed_agent) -> None:
    crud_managed_agent_enrollment.create_for_agent(
        db_session,
        account_id=test_user.account_id,
        agent_id=managed_agent.id,
        created_by_user_id=test_user.id,
        enrollment_type="runtime_plugin_control",
        adapter_key="openclaw",
        managed_config={},
        validation_result={
            "control_channel_configured": True,
            "control_plugin_verified": True,
            "control_ws_url_ok": True,
            "control_bearer_token_ok": True,
        },
    )


def test_agent_control_ws_rejects_missing_runtime_token(client):
    """Managed-agent control WebSocket requires a runtime bearer token."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/agents/control/ws"):
            pass

    assert exc_info.value.code == 1008


def test_agent_control_ws_connects_and_updates_presence(client, db_session, test_user):
    """Runtime bearer token should bind the WebSocket to agent and session IDs."""
    token_body = _issue_runtime_token(client)
    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="openclaw",
        session_source_id="openclaw-live",
    )
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-live",
    )
    assert runtime_session is not None
    assert managed_agent is not None

    old_seen_at = datetime.now(UTC) - timedelta(hours=1)
    runtime_session.last_activity_at = old_seen_at
    managed_agent.last_seen_at = old_seen_at
    db_session.add(runtime_session)
    db_session.add(managed_agent)
    db_session.commit()

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        connected = websocket.receive_json()
        assert connected["type"] == "presence"
        assert connected["name"] == "connected"
        assert connected["managed_agent_id"] == str(managed_agent.id)
        assert connected["runtime_session_id"] == str(runtime_session.id)
        assert connected["session_source_type"] == "openclaw"

        websocket.send_json({"type": "heartbeat", "message_id": "hb-1", "payload": {}})
        ack = websocket.receive_json()
        assert ack["type"] == "ack"
        assert ack["name"] == "heartbeat"
        assert ack["message_id"] == "hb-1"

        db_session.expire_all()
        refreshed_agent = crud_managed_agent.get_for_account(
            db_session,
            account_id=str(test_user.account_id),
            agent_id=str(managed_agent.id),
        )
        refreshed_session = crud_runtime_session.get_account_session(
            db_session,
            account_id=str(test_user.account_id),
            runtime_session_id=str(runtime_session.id),
        )
        assert refreshed_agent is not None
        assert refreshed_session is not None
        assert refreshed_agent.runtime_session_id == runtime_session.id
        assert refreshed_agent.last_seen_at > old_seen_at.replace(tzinfo=None)
        assert refreshed_session.last_activity_at > old_seen_at.replace(tzinfo=None)


def test_agent_control_ws_runtime_token_can_reconnect(client, db_session, test_user):
    """A transient disconnect should not invalidate the runtime bearer token."""
    token_body = _issue_runtime_token(client, session_source_id="openclaw-reconnect")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-reconnect",
    )
    assert managed_agent is not None

    url = f"/api/v1/agents/control/ws?token={token_body['token']}"
    with client.websocket_connect(url) as websocket:
        assert websocket.receive_json()["type"] == "presence"

    db_session.expire_all()
    disconnected_agent = crud_managed_agent.get_for_account(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(managed_agent.id),
    )
    assert disconnected_agent is not None
    assert disconnected_agent.runtime_session_id is None

    with client.websocket_connect(url) as websocket:
        reconnected = websocket.receive_json()
        assert reconnected["type"] == "presence"
        assert reconnected["managed_agent_id"] == str(managed_agent.id)


def test_agent_control_ws_runtime_token_can_rebind_stale_agent_session(
    client, db_session, test_user
):
    """Re-onboarded control tokens should recover from stale agent bindings."""
    token_body = _issue_runtime_token(client, session_source_id="openclaw-rebound")
    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="openclaw",
        session_source_id="openclaw-rebound",
    )
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-rebound",
    )
    assert runtime_session is not None
    assert managed_agent is not None

    stale_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="openclaw",
        session_source_id="openclaw-stale-binding",
        runtime_principal_type="openclaw",
        runtime_principal_id="openclaw-stale-binding",
        runtime_principal_name="Stale OpenClaw",
        started_at=datetime.now(UTC) - timedelta(minutes=10),
        last_activity_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    managed_agent.runtime_session_id = stale_session.id
    db_session.add(managed_agent)
    db_session.commit()

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        connected = websocket.receive_json()
        assert connected["type"] == "presence"
        assert connected["managed_agent_id"] == str(managed_agent.id)
        assert connected["runtime_session_id"] == str(runtime_session.id)

    db_session.expire_all()
    rebound_agent = crud_managed_agent.get_for_account(
        db_session,
        account_id=str(test_user.account_id),
        agent_id=str(managed_agent.id),
    )
    assert rebound_agent is not None
    assert rebound_agent.runtime_session_id is None


def test_controllable_agents_list_and_detail_expose_capabilities(
    client, db_session, test_user
):
    """Web/mobile clients should see explicit Agent Control capabilities."""
    _issue_runtime_token(client, session_source_id="openclaw-capabilities")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-capabilities",
    )
    assert managed_agent is not None
    _mark_agent_control_configured(db_session, test_user, managed_agent)

    list_response = client.get("/api/v1/agents/control")
    assert list_response.status_code == 200
    items = list_response.json()["items"]
    item = next(agent for agent in items if agent["id"] == str(managed_agent.id))
    assert item["control_feature_name"] == "Agent Control"
    assert item["control_enabled"] is True
    assert item["control_online"] is True
    assert item["supports_new_session"] is True
    assert item["supports_existing_session"] is True
    assert item["supports_voice"] is True
    assert item["supports_interrupt"] is False
    assert "send_text_prompt" in item["control_capabilities"]
    assert item["supported_input_modes"] == ["text", "voice_transcript"]

    detail_response = client.get(f"/api/v1/agents/{managed_agent.id}")
    assert detail_response.status_code == 200
    detail_agent = detail_response.json()["agent"]
    assert detail_agent["control_enabled"] is True
    assert detail_agent["control_online"] is True
    assert detail_agent["control_state"] == "plugin_connected"


def test_control_capabilities_require_explicit_plugin_config(
    client, db_session, test_user
):
    """Active agents must not look controllable until the runtime plugin is installed."""
    _issue_runtime_token(client, session_source_id="openclaw-without-control")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-without-control",
    )
    assert managed_agent is not None

    list_response = client.get("/api/v1/agents/control")
    assert list_response.status_code == 200
    assert all(
        agent["id"] != str(managed_agent.id) for agent in list_response.json()["items"]
    )

    detail_response = client.get(f"/api/v1/agents/{managed_agent.id}")
    assert detail_response.status_code == 200
    detail_agent = detail_response.json()["agent"]
    assert detail_agent["control_enabled"] is False
    assert detail_agent["control_capabilities"] == []

    command_response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/commands",
        json={"message": "This should not route yet"},
    )
    assert command_response.status_code == 409
    assert "Agent Control plugin" in command_response.json()["detail"]


def test_control_config_without_plugin_validation_is_install_pending(
    client, db_session, test_user
):
    """CLI-written config should not look online until plugin validation passes."""
    _issue_runtime_token(client, session_source_id="openclaw-install-pending")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-install-pending",
    )
    assert managed_agent is not None
    _mark_agent_control_install_pending(db_session, test_user, managed_agent)

    list_response = client.get("/api/v1/agents/control")
    assert list_response.status_code == 200
    items = list_response.json()["items"]
    item = next(agent for agent in items if agent["id"] == str(managed_agent.id))
    assert item["control_state"] == "install_pending"
    assert item["control_enabled"] is False
    assert item["control_online"] is False
    assert item["control_capabilities"] == []

    detail_response = client.get(f"/api/v1/agents/{managed_agent.id}")
    assert detail_response.status_code == 200
    detail_agent = detail_response.json()["agent"]
    assert detail_agent["control_state"] == "install_pending"
    assert detail_agent["control_enabled"] is False

    command_response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/commands",
        json={"message": "This should wait for plugin validation"},
    )
    assert command_response.status_code == 409
    assert "Agent Control plugin" in command_response.json()["detail"]


def test_capabilities_envelope_verifies_pending_cli_control_config(
    client, db_session, test_user
):
    """A live runtime plugin should promote CLI control config from pending."""
    token_body = _issue_runtime_token(
        client, session_source_id="openclaw-runtime-ready"
    )
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-runtime-ready",
    )
    assert managed_agent is not None
    _mark_agent_control_install_pending(db_session, test_user, managed_agent)

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        assert websocket.receive_json()["type"] == "presence"
        websocket.send_json(
            {
                "type": "presence",
                "name": "capabilities",
                "message_id": "caps-1",
                "payload": {
                    "status": "online",
                    "capabilities": {
                        "new_session": True,
                        "existing_session": True,
                        "text": True,
                        "voice": True,
                    },
                },
            }
        )
        websocket.send_json(
            {"type": "heartbeat", "message_id": "hb-after-caps", "payload": {}}
        )
        assert websocket.receive_json()["name"] == "heartbeat"

        db_session.expire_all()
        list_response = client.get("/api/v1/agents/control")
        assert list_response.status_code == 200
        item = next(
            agent
            for agent in list_response.json()["items"]
            if agent["id"] == str(managed_agent.id)
        )
        assert item["control_enabled"] is True
        assert item["control_online"] is True
        assert item["control_state"] == "plugin_connected"
        assert "send_text_prompt" in item["control_capabilities"]


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_runtime_control_validation_survives_later_cli_enrollment(
    mock_get_nats_client,
    client,
    db_session,
    test_user,
):
    """Runtime evidence and CLI config can arrive in either order."""
    _issue_runtime_token(client, session_source_id="openclaw-control-race")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-control-race",
    )
    assert managed_agent is not None
    _mark_runtime_control_verified(db_session, test_user, managed_agent)
    _mark_agent_control_install_pending(db_session, test_user, managed_agent)

    list_response = client.get("/api/v1/agents/control")
    assert list_response.status_code == 200
    item = next(
        agent
        for agent in list_response.json()["items"]
        if agent["id"] == str(managed_agent.id)
    )
    assert item["control_enabled"] is True
    assert item["control_online"] is True
    assert item["control_state"] == "plugin_connected"

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats

    command_response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/commands",
        json={"message": "Keep working"},
    )
    assert command_response.status_code == 202
    mock_nats.publish.assert_awaited_once()


def test_capabilities_envelope_creates_plugin_control_enrollment_without_cli(
    client, db_session, test_user
):
    """Standalone plugin onboarding should not require a prior CLI enrollment."""
    token_body = _issue_runtime_token(client, session_source_id="openclaw-plugin-only")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-plugin-only",
    )
    assert managed_agent is not None

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        assert websocket.receive_json()["type"] == "presence"
        websocket.send_json(
            {
                "type": "presence",
                "name": "capabilities",
                "message_id": "caps-standalone",
                "payload": {
                    "status": "online",
                    "capabilities": {"text": True, "voice": True},
                },
            }
        )
        websocket.send_json(
            {"type": "heartbeat", "message_id": "hb-standalone", "payload": {}}
        )
        assert websocket.receive_json()["name"] == "heartbeat"

        db_session.expire_all()
        enrollment = crud_managed_agent_enrollment.get_latest_for_agent_by_type(
            db_session,
            account_id=str(test_user.account_id),
            agent_id=str(managed_agent.id),
            enrollment_type="runtime_plugin_control",
        )
        assert enrollment is not None
        assert enrollment.validation_result["control_plugin_verified"] is True

        list_response = client.get("/api/v1/agents/control")
        item = next(
            agent
            for agent in list_response.json()["items"]
            if agent["id"] == str(managed_agent.id)
        )
        assert item["control_enabled"] is True
        assert item["control_online"] is True


def test_agent_control_ws_command_result_is_persisted(
    client,
    db_session,
    test_user,
):
    """Runtime command results should become durable chat history."""
    token_body = _issue_runtime_token(client, session_source_id="openclaw-result")
    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="openclaw",
        session_source_id="openclaw-result",
    )
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-result",
    )
    assert runtime_session is not None
    assert managed_agent is not None

    crud_runtime_session_activity.log_agent_control_message(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
        message="Please acknowledge this command",
        status="queued",
        metadata={
            "command_id": "cmd-result-1",
            "managed_agent_id": str(managed_agent.id),
        },
    )

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        assert websocket.receive_json()["type"] == "presence"
        websocket.send_json(
            {
                "type": "status",
                "name": "command_result",
                "message_id": "result-1",
                "payload": {
                    "command_id": "cmd-result-1",
                    "status": "completed",
                    "reply_text": "ACK",
                    "exit_code": 0,
                },
            }
        )
        websocket.send_json({"type": "heartbeat", "message_id": "hb-2", "payload": {}})
        assert websocket.receive_json()["name"] == "heartbeat"

    activity = crud_runtime_session_activity.list_for_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
    )
    command_activity = [
        item
        for item in activity
        if item.activity_type == "agent_control_message"
        and item.metadata_["command_id"] == "cmd-result-1"
    ]
    assert len(command_activity) == 2
    assert {item.summary for item in command_activity} == {
        "Please acknowledge this command",
        "ACK",
    }
    operator_message = next(
        item
        for item in command_activity
        if item.summary == "Please acknowledge this command"
    )
    agent_reply = next(item for item in command_activity if item.summary == "ACK")
    assert operator_message.status == "completed"
    assert agent_reply.status == "completed"
    assert agent_reply.metadata_["role"] == "assistant"


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_agent_control_command_publishes_to_agent_subject(
    mock_get_nats_client,
    client,
    db_session,
    test_user,
):
    """Operator text commands should publish a typed command envelope."""
    _issue_runtime_token(client, session_source_id="openclaw-command")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-command",
    )
    assert managed_agent is not None
    _mark_agent_control_configured(db_session, test_user, managed_agent)

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats

    response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/commands",
        json={
            "message": "Can you inspect the failing test?",
            "metadata": {"via": "ui"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["managed_agent_id"] == str(managed_agent.id)
    assert body["published"] is True
    assert body["local_delivery"] is False
    assert body["session_mode"] == "current"
    assert body["subject"] == f"agent-control.commands.{managed_agent.id}"
    assert body["command_envelope"]["payload"]["session_mode"] == "current"

    mock_nats.publish.assert_awaited_once()
    subject, payload = mock_nats.publish.await_args.args
    assert subject == f"agent-control.commands.{managed_agent.id}"
    envelope = json.loads(payload.decode("utf-8"))
    assert envelope["type"] == "command"
    assert envelope["name"] == "send_message"
    assert envelope["payload"]["text"] == "Can you inspect the failing test?"
    assert envelope["payload"]["metadata"] == {"via": "ui"}
    assert envelope["payload"]["input_mode"] == "text"

    activity = crud_runtime_session_activity.list_for_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=managed_agent.runtime_session_id,
    )
    command_activity = [
        item for item in activity if item.activity_type == "agent_control_message"
    ]
    assert len(command_activity) == 1
    assert command_activity[0].summary == "Can you inspect the failing test?"
    assert command_activity[0].status == "queued"
    assert command_activity[0].metadata_["command_id"] == body["command_id"]


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_agent_control_prompt_targets_existing_session(
    mock_get_nats_client,
    client,
    db_session,
    test_user,
):
    """Prompt route should expose existing-session routing semantics."""
    _issue_runtime_token(client, session_source_id="openclaw-existing-session")
    runtime_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="openclaw",
        session_source_id="openclaw-existing-session",
    )
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-existing-session",
    )
    assert runtime_session is not None
    assert managed_agent is not None
    _mark_agent_control_configured(db_session, test_user, managed_agent)

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats

    response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/prompts",
        json={
            "message": "Continue the current task",
            "target_session_id": str(runtime_session.id),
            "metadata": {"source": "web"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["session_mode"] == "existing"
    assert body["target_session_id"] == str(runtime_session.id)
    payload = body["command_envelope"]["payload"]
    assert payload["session_mode"] == "existing"
    assert payload["target_session_id"] == str(runtime_session.id)
    assert payload["session_source_id"] == runtime_session.session_source_id
    assert payload["session_reference"] == runtime_session.session_reference
    assert payload["start_new_session"] is False
    assert body["session_source_id"] == runtime_session.session_source_id
    assert body["session_reference"] == runtime_session.session_reference

    activity = crud_runtime_session_activity.list_for_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=runtime_session.id,
    )
    command_activity = [
        item for item in activity if item.activity_type == "agent_control_message"
    ]
    assert len(command_activity) == 1
    assert command_activity[0].summary == "Continue the current task"
    assert command_activity[0].metadata_["session_mode"] == "existing"
    assert command_activity[0].metadata_["target_session_id"] == str(runtime_session.id)


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_agent_control_existing_session_envelope_includes_native_ids(
    mock_get_nats_client,
    client,
    db_session,
    test_user,
):
    """Existing-session commands persist the target's native resume identity.

    Clients keep sending the Preloop runtime-session UUID. The sidecar
    resumes by session_source_id, so the outbound envelope must carry that
    native id (and session_reference) even when they differ from the
    managed agent's durable source id.
    """
    token_response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "claude_code",
            "session_source_id": "claude-principal-host",
            "session_reference": "/tmp/claude.json",
            "runtime_principal_name": "Claude Code",
        },
    )
    assert token_response.status_code == 201
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="claude_code",
        session_source_id="claude-principal-host",
    )
    assert managed_agent is not None
    _mark_agent_control_configured(db_session, test_user, managed_agent)

    native_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="ses_native_abc123",
        session_reference="resume:ses_native_abc123",
        runtime_principal_type="claude_code",
        runtime_principal_id="claude-principal-host",
        runtime_principal_name="Claude Code",
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
    )
    db_session.commit()

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats

    response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/commands",
        json={
            "message": "Resume the ended investigation",
            "target_session_id": str(native_session.id),
            "session_source_id": "client-must-not-win",
            "session_reference": "client-supplied-ref",
            "metadata": {"source": "ios"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["session_mode"] == "existing"
    assert body["target_session_id"] == str(native_session.id)
    assert body["session_source_id"] == "ses_native_abc123"
    assert body["session_reference"] == "resume:ses_native_abc123"
    payload = body["command_envelope"]["payload"]
    assert payload["target_session_id"] == str(native_session.id)
    assert payload["session_source_id"] == "ses_native_abc123"
    assert payload["session_reference"] == "resume:ses_native_abc123"

    record = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=test_user.account_id,
        command_id=body["command_id"],
    )
    assert record is not None
    persisted = record.envelope["payload"]
    assert persisted["target_session_id"] == str(native_session.id)
    assert persisted["session_source_id"] == "ses_native_abc123"
    assert persisted["session_reference"] == "resume:ses_native_abc123"


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_agent_control_prompt_can_request_new_session(
    mock_get_nats_client,
    client,
    db_session,
    test_user,
):
    """Prompt route should let adapters create a new controlled session."""
    _issue_runtime_token(client, session_source_id="openclaw-new-session")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-new-session",
    )
    assert managed_agent is not None
    _mark_agent_control_configured(db_session, test_user, managed_agent)

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats

    response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/prompts",
        json={
            "message": "Start a fresh investigation",
            "start_new_session": True,
            "metadata": {"source": "mobile"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["session_mode"] == "new"
    assert body["target_session_id"] is not None
    assert body["target_session_id"] != body["runtime_session_id"]
    payload = body["command_envelope"]["payload"]
    assert payload["session_mode"] == "new"
    assert payload["start_new_session"] is True
    assert payload["target_session_id"] is None

    existing_activity = crud_runtime_session_activity.list_for_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=body["runtime_session_id"],
    )
    assert all(
        item.summary != "Start a fresh investigation" for item in existing_activity
    )

    new_session_activity = crud_runtime_session_activity.list_for_runtime_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=body["target_session_id"],
    )
    command_activity = [
        item
        for item in new_session_activity
        if item.activity_type == "agent_control_message"
    ]
    assert len(command_activity) == 1
    assert command_activity[0].summary == "Start a fresh investigation"
    assert command_activity[0].metadata_["session_mode"] == "new"
    assert command_activity[0].metadata_["start_new_session"] is True


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_agent_control_voice_transcript_alias_routes_prompt(
    mock_get_nats_client,
    client,
    db_session,
    test_user,
):
    """Mobile voice alias should use the same command envelope shape."""
    _issue_runtime_token(client, session_source_id="openclaw-voice")
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id="openclaw-voice",
    )
    assert managed_agent is not None
    _mark_agent_control_configured(db_session, test_user, managed_agent)

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats

    response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/voice-transcripts",
        json={
            "transcript": "Summarize what you are doing",
            "voice": {"locale": "en-US", "duration_ms": 2500},
            "metadata": {"device": "watch"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    payload = body["command_envelope"]["payload"]
    assert payload["text"] == "Summarize what you are doing"
    assert payload["input_mode"] == "voice_transcript"
    assert payload["voice"] == {"locale": "en-US", "duration_ms": 2500}
    assert payload["metadata"] == {"device": "watch"}
