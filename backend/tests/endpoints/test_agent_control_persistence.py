"""Durable command persistence tests for the managed-agent control plane."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from preloop.models.crud import (
    crud_agent_control_command,
    crud_managed_agent,
    crud_managed_agent_enrollment,
)


def _issue_runtime_token(client, *, session_source_id: str):
    response = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "openclaw",
            "session_source_id": session_source_id,
            "session_reference": "/tmp/openclaw.json",
            "runtime_principal_name": "OpenClaw Persistence Agent",
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
                }
            }
        },
        validation_result={
            "control_channel_configured": True,
            "control_ws_url_ok": True,
            "control_bearer_token_ok": True,
        },
    )


def _setup_controllable_agent(client, db_session, test_user, *, session_source_id):
    token_body = _issue_runtime_token(client, session_source_id=session_source_id)
    managed_agent = crud_managed_agent.get_by_source(
        db_session,
        account_id=str(test_user.account_id),
        session_source_type="openclaw",
        session_source_id=session_source_id,
    )
    assert managed_agent is not None
    _mark_agent_control_configured(db_session, test_user, managed_agent)
    return managed_agent, token_body


def _persist_command(
    db_session,
    test_user,
    managed_agent,
    *,
    command_id=None,
    text="persisted command",
    expires_at=None,
):
    command_id = command_id or str(uuid.uuid4())
    envelope = {
        "type": "command",
        "name": "send_message",
        "message_id": command_id,
        "account_id": str(test_user.account_id),
        "managed_agent_id": str(managed_agent.id),
        "runtime_session_id": str(managed_agent.runtime_session_id),
        "session_source_type": managed_agent.session_source_type,
        "session_source_id": managed_agent.session_source_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": {"text": text, "session_mode": "current"},
    }
    return crud_agent_control_command.create_command(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=managed_agent.id,
        runtime_session_id=managed_agent.runtime_session_id,
        command_id=command_id,
        envelope=envelope,
        source="console",
        created_by_user_id=test_user.id,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=1)),
    )


# ---------------------------------------------------------------------------
# Endpoint: persist-before-delivery and status reporting
# ---------------------------------------------------------------------------


def test_command_is_persisted_before_delivery_and_marked_delivered(
    client, db_session, test_user
):
    """The command row must exist (pending) before the WS send happens."""
    managed_agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="openclaw-persist-local"
    )

    observed_status = {}

    async def _fake_send(*, managed_agent_id, envelope):
        record = crud_agent_control_command.get_by_command_id(
            db_session,
            account_id=test_user.account_id,
            command_id=envelope.message_id,
        )
        observed_status["at_delivery_time"] = record.status if record else None
        return True

    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        side_effect=_fake_send,
    ):
        response = client.post(
            f"/api/v1/agents/{managed_agent.id}/control/commands",
            json={"message": "Please continue", "metadata": {"source": "console"}},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["local_delivery"] is True
    assert body["command_status"] == "delivered"
    # Persisted BEFORE the delivery attempt.
    assert observed_status["at_delivery_time"] == "pending"

    record = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=test_user.account_id,
        command_id=body["command_id"],
    )
    assert record is not None
    assert record.status == "delivered"
    assert record.delivered_at is not None
    assert record.acked_at is None
    assert record.expires_at is not None
    assert record.envelope["payload"]["text"] == "Please continue"
    assert record.envelope["message_id"] == body["command_id"]
    assert record.source == "console"
    assert record.created_by_user_id == test_user.id
    assert record.managed_agent_id == managed_agent.id


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_command_stays_pending_when_only_published_to_nats(
    mock_get_nats_client, client, db_session, test_user
):
    """NATS fan-out is best-effort: the row stays pending until a WS send."""
    managed_agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="openclaw-persist-queued"
    )

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock()
    mock_get_nats_client.return_value = mock_nats

    response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/commands",
        json={"message": "Queued while offline"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["local_delivery"] is False
    assert body["published"] is True
    assert body["command_status"] == "pending"

    record = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=test_user.account_id,
        command_id=body["command_id"],
    )
    assert record is not None
    assert record.status == "pending"
    assert record.delivered_at is None


@patch("preloop.api.endpoints.agent_control.get_nats_client")
def test_command_marked_failed_when_no_channel_available(
    mock_get_nats_client, client, db_session, test_user
):
    """No local socket and no NATS: 503, and the row records the failure."""
    managed_agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="openclaw-persist-failed"
    )
    mock_get_nats_client.return_value = None

    response = client.post(
        f"/api/v1/agents/{managed_agent.id}/control/commands",
        json={"message": "Nobody is listening"},
    )

    assert response.status_code == 503
    records = crud_agent_control_command.list_recent_for_agent(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=managed_agent.id,
    )
    assert len(records) == 1
    assert records[0].status == "failed"
    assert "unavailable" in records[0].last_error


# ---------------------------------------------------------------------------
# WebSocket: redelivery on reconnect and end-to-end acks
# ---------------------------------------------------------------------------


def test_ws_reconnect_redelivers_pending_commands_in_order(
    client, db_session, test_user
):
    """Pending commands are replayed verbatim (original ids) on reconnect."""
    managed_agent, token_body = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="openclaw-redeliver"
    )

    first = _persist_command(db_session, test_user, managed_agent, text="first")
    second = _persist_command(db_session, test_user, managed_agent, text="second")
    expired = _persist_command(
        db_session,
        test_user,
        managed_agent,
        text="too old",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    already_delivered = _persist_command(
        db_session, test_user, managed_agent, text="already delivered"
    )
    crud_agent_control_command.mark_delivered(
        db_session,
        account_id=test_user.account_id,
        command_id=already_delivered.command_id,
        delivered_at=datetime.now(UTC),
    )

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        assert websocket.receive_json()["type"] == "presence"
        replay_one = websocket.receive_json()
        replay_two = websocket.receive_json()

    # Redelivered verbatim, in created_at order, keeping the original
    # command ids so idempotent runtime plugins can dedupe.
    assert replay_one["type"] == "command"
    assert replay_one["message_id"] == first.command_id
    assert replay_one["payload"]["text"] == "first"
    assert replay_two["message_id"] == second.command_id

    db_session.expire_all()
    assert (
        crud_agent_control_command.get_by_command_id(
            db_session, account_id=test_user.account_id, command_id=first.command_id
        ).status
        == "delivered"
    )
    assert (
        crud_agent_control_command.get_by_command_id(
            db_session, account_id=test_user.account_id, command_id=second.command_id
        ).status
        == "delivered"
    )
    assert (
        crud_agent_control_command.get_by_command_id(
            db_session, account_id=test_user.account_id, command_id=expired.command_id
        ).status
        == "expired"
    )


def test_ws_inbound_ack_marks_command_acked(client, db_session, test_user):
    """An inbound ack envelope transitions delivered -> acked."""
    managed_agent, token_body = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="openclaw-ack"
    )
    record = _persist_command(db_session, test_user, managed_agent, text="ack me")

    with client.websocket_connect(
        f"/api/v1/agents/control/ws?token={token_body['token']}"
    ) as websocket:
        assert websocket.receive_json()["type"] == "presence"
        # The pending command is redelivered on connect.
        replay = websocket.receive_json()
        assert replay["message_id"] == record.command_id

        websocket.send_json(
            {
                "type": "status",
                "name": "command_ack",
                "message_id": "ack-1",
                "payload": {"command_id": record.command_id},
            }
        )
        # Unknown ids are tolerated (logged, not an error).
        websocket.send_json(
            {
                "type": "status",
                "name": "command_ack",
                "message_id": "ack-2",
                "payload": {"command_id": "does-not-exist"},
            }
        )
        websocket.send_json(
            {"type": "heartbeat", "message_id": "hb-ack", "payload": {}}
        )
        assert websocket.receive_json()["name"] == "heartbeat"

    db_session.expire_all()
    refreshed = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=test_user.account_id,
        command_id=record.command_id,
    )
    assert refreshed.status == "acked"
    assert refreshed.acked_at is not None
    assert refreshed.delivered_at is not None


# ---------------------------------------------------------------------------
# CRUD state machine
# ---------------------------------------------------------------------------


def test_crud_state_machine_transitions(client, db_session, test_user):
    """pending -> delivered -> acked; failed and expired side states."""
    managed_agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="openclaw-crud"
    )
    now = datetime.now(UTC)

    record = _persist_command(db_session, test_user, managed_agent)
    assert record.status == "pending"

    delivered = crud_agent_control_command.mark_delivered(
        db_session,
        account_id=test_user.account_id,
        command_id=record.command_id,
        delivered_at=now,
    )
    assert delivered.status == "delivered"
    first_delivered_at = delivered.delivered_at
    # Idempotent: a second delivery mark keeps the original timestamp.
    redelivered = crud_agent_control_command.mark_delivered(
        db_session,
        account_id=test_user.account_id,
        command_id=record.command_id,
        delivered_at=now + timedelta(minutes=5),
    )
    assert redelivered.delivered_at == first_delivered_at

    acked = crud_agent_control_command.mark_acked(
        db_session,
        account_id=test_user.account_id,
        command_id=record.command_id,
        acked_at=now + timedelta(minutes=1),
    )
    assert acked.status == "acked"
    assert acked.acked_at is not None

    # Acked commands never regress to failed.
    failed = crud_agent_control_command.mark_failed(
        db_session,
        account_id=test_user.account_id,
        command_id=record.command_id,
        error="late failure",
    )
    assert failed.status == "acked"

    # Unknown ids resolve to None (tolerant callers log and continue).
    assert (
        crud_agent_control_command.mark_acked(
            db_session,
            account_id=test_user.account_id,
            command_id="missing",
            acked_at=now,
        )
        is None
    )

    # An ack on a still-pending row implies delivery.
    pending_then_acked = _persist_command(db_session, test_user, managed_agent)
    acked_direct = crud_agent_control_command.mark_acked(
        db_session,
        account_id=test_user.account_id,
        command_id=pending_then_acked.command_id,
        acked_at=now,
    )
    assert acked_direct.status == "acked"
    assert acked_direct.delivered_at is not None


def test_crud_expire_stale_and_undelivered_query(client, db_session, test_user):
    """expire_stale flips stale pending rows; redelivery sees pending only."""
    managed_agent, _ = _setup_controllable_agent(
        client, db_session, test_user, session_source_id="openclaw-expiry"
    )
    now = datetime.now(UTC)

    fresh_one = _persist_command(db_session, test_user, managed_agent, text="fresh one")
    fresh_two = _persist_command(db_session, test_user, managed_agent, text="fresh two")
    stale = _persist_command(
        db_session,
        test_user,
        managed_agent,
        text="stale",
        expires_at=now - timedelta(seconds=1),
    )
    delivered = _persist_command(db_session, test_user, managed_agent, text="delivered")
    crud_agent_control_command.mark_delivered(
        db_session,
        account_id=test_user.account_id,
        command_id=delivered.command_id,
        delivered_at=now,
    )

    expired_count = crud_agent_control_command.expire_stale(db_session, now=now)
    assert expired_count == 1
    db_session.expire_all()
    assert (
        crud_agent_control_command.get_by_command_id(
            db_session, account_id=test_user.account_id, command_id=stale.command_id
        ).status
        == "expired"
    )

    undelivered = crud_agent_control_command.get_undelivered_for_agent(
        db_session, managed_agent_id=managed_agent.id, now=now
    )
    # Pending-only, in created_at order; delivered/expired rows excluded.
    assert [row.command_id for row in undelivered] == [
        fresh_one.command_id,
        fresh_two.command_id,
    ]

    recent = crud_agent_control_command.list_recent_for_agent(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=managed_agent.id,
        limit=2,
    )
    assert len(recent) == 2
    assert recent[0].command_id == delivered.command_id
