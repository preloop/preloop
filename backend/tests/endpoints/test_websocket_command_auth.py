"""Authorization of execution commands sent over WebSockets."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution, crud_user
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import Decision
from preloop.services.flow_execution_stop import MANUAL_STOP_MESSAGE, TeardownOutcome

WS = "preloop.api.endpoints.websockets"


@pytest.fixture(autouse=True)
def _no_hooks():
    account_hooks.reset_account_hooks()
    yield
    account_hooks.reset_account_hooks()


@pytest.fixture
def execution(db_session, test_user):
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"WS Flow {uuid.uuid4().hex[:8]}",
            trigger_event_source="github",
            trigger_event_types=["push"],
            prompt_template="Review",
            agent_type="openhands",
            agent_config={},
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    return crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )


@pytest.fixture
def other_user(db_session):
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    return crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": "other@example.com",
            "username": "otheruser",
            "full_name": "Other",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )


def _deny_execute_flows():
    def authorizer(ctx, action, resource):
        if action == "execute_flows":
            return Decision("deny", reason="no execute")
        return Decision("allow")

    account_hooks.register_authorizer(authorizer)


@contextmanager
def _ws_env(db_session, token_user):
    """Route the endpoint's DB and NATS access to the test session and a mock."""
    nats = MagicMock()
    nats.is_connected = True
    nats.publish = AsyncMock()

    def _session():
        yield db_session

    async def _run_db(op):
        return op(db_session)

    session = MagicMock(id="s", connection_id="c", is_authenticated=False)
    event_bus = MagicMock()
    event_bus.connect = AsyncMock()
    event_bus.close = AsyncMock()
    event_bus.nc.subscribe = AsyncMock(return_value=MagicMock(unsubscribe=AsyncMock()))
    with (
        patch(f"{WS}.get_db_session", _session),
        patch(f"{WS}._safe_close_db_session", lambda db: None),
        patch(f"{WS}.run_db_async", _run_db),
        patch(f"{WS}.get_nats_client", AsyncMock(return_value=nats)),
        patch(f"{WS}._resolve_token_user", AsyncMock(return_value=token_user)),
        patch(
            "preloop.api.middleware.websocket_auth.WebSocketAuthMiddleware._validate_token",
            AsyncMock(return_value=token_user),
        ),
        patch(f"{WS}.EventBus", MagicMock(return_value=event_bus)),
        patch(f"{WS}.session_manager") as sm,
        patch(f"{WS}.manager") as mgr,
        patch(f"{WS}._set_approval_visibility", AsyncMock()),
        patch(
            "preloop.services.flow_execution_stop._tear_down_runtime",
            AsyncMock(return_value=TeardownOutcome(confirmed=True)),
        ),
    ):
        sm.create_session = AsyncMock(return_value=session)
        sm.upgrade_session = AsyncMock()
        sm.end_session = AsyncMock()
        sm.update_activity = MagicMock()
        mgr.connect_with_account = AsyncMock(return_value="m")
        mgr.get_subscriptions = MagicMock(return_value=set())
        mgr.active_connections = {}
        yield nats


def _unified_command(client, execution_id, authenticate):
    with client.websocket_connect("/api/v1/ws/unified") as ws:
        assert ws.receive_json()["type"] == "handshake"
        if authenticate:
            ws.send_json({"type": "authenticate", "token": "t"})
            assert ws.receive_json()["type"] == "authenticated"
        ws.send_json(
            {"type": "command", "command": "stop", "execution_id": execution_id}
        )
        return ws.receive_json()


def _assert_unauthorized(reply, execution_id):
    assert reply == {
        "type": "command_error",
        "execution_id": execution_id,
        "error": "unauthorized",
    }


def test_unified_anonymous_command_is_rejected(client, db_session, execution):
    with _ws_env(db_session, None) as nats:
        reply = _unified_command(client, str(execution.id), authenticate=False)
    _assert_unauthorized(reply, str(execution.id))
    nats.publish.assert_not_called()
    db_session.refresh(execution)
    assert execution.status == "RUNNING"


def test_unified_other_account_command_is_rejected(
    client, db_session, execution, other_user
):
    with _ws_env(db_session, other_user) as nats:
        reply = _unified_command(client, str(execution.id), authenticate=True)
    _assert_unauthorized(reply, str(execution.id))
    nats.publish.assert_not_called()
    db_session.refresh(execution)
    assert execution.status == "RUNNING"


def test_unified_unknown_execution_matches_not_owned(client, db_session, test_user):
    missing = str(uuid.uuid4())
    with _ws_env(db_session, test_user) as nats:
        reply = _unified_command(client, missing, authenticate=True)
    _assert_unauthorized(reply, missing)
    nats.publish.assert_not_called()


def test_unified_owner_without_permission_is_rejected(
    client, db_session, execution, test_user
):
    _deny_execute_flows()
    with _ws_env(db_session, test_user) as nats:
        reply = _unified_command(client, str(execution.id), authenticate=True)
    _assert_unauthorized(reply, str(execution.id))
    nats.publish.assert_not_called()


def test_unified_owner_stop_goes_through_stop_service(
    client, db_session, execution, test_user
):
    with _ws_env(db_session, test_user) as nats:
        reply = _unified_command(client, str(execution.id), authenticate=True)
    assert reply["type"] == "command_ack"
    assert reply["status"] == "stopped"
    nats.publish.assert_awaited_once()
    assert nats.publish.await_args.args[0] == f"flow-commands.{execution.id}"
    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.error_message == MANUAL_STOP_MESSAGE


def _execution_socket_command(client, execution_id, command="stop"):
    url = f"/api/v1/ws/flow-executions/{execution_id}?token=t"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"command": command, "message": "hi"})
        return ws.receive_json()


def test_execution_socket_without_permission_is_rejected(
    client, db_session, execution, test_user
):
    _deny_execute_flows()
    with _ws_env(db_session, test_user) as nats:
        reply = _execution_socket_command(client, execution.id)
    _assert_unauthorized(reply, str(execution.id))
    nats.publish.assert_not_called()
    db_session.refresh(execution)
    assert execution.status == "RUNNING"


def test_execution_socket_owner_with_permission_publishes(
    client, db_session, execution, test_user
):
    with _ws_env(db_session, test_user) as nats:
        reply = _execution_socket_command(client, execution.id, "send_message")
    assert reply["type"] == "command_ack"
    assert reply["status"] == "command_sent"
    nats.publish.assert_awaited_once()
    assert nats.publish.await_args.args[0] == f"flow-commands.{execution.id}"


def test_execution_socket_command_failure_is_sanitized(
    client, db_session, execution, test_user
):
    with _ws_env(db_session, test_user) as nats:
        nats.publish.side_effect = RuntimeError("internal detail")
        reply = _execution_socket_command(client, execution.id, "send_message")
    assert reply == {
        "type": "command_error",
        "execution_id": str(execution.id),
        "error": "failed",
    }


@pytest.mark.asyncio
async def test_command_lookup_runs_off_loop_and_releases_transaction(
    db_session, execution, test_user
):
    from preloop.api.endpoints import websockets as ws

    calls = []

    async def _off_loop(op):
        calls.append("off_loop")
        return op()

    def _session():
        yield db_session

    nats = MagicMock(is_connected=True, publish=AsyncMock())
    with (
        patch(f"{WS}.get_db_session", _session),
        patch(f"{WS}._safe_close_db_session", lambda db: None),
        patch(f"{WS}.run_db_off_loop", _off_loop),
        patch(f"{WS}.release_transaction", lambda db: calls.append("release")),
        patch(f"{WS}.get_nats_client", AsyncMock(return_value=nats)),
    ):
        result = await ws._run_execution_command(
            test_user, execution.id, {"command": "send_message"}
        )
    assert result == {"status": "command_sent"}
    assert calls == ["off_loop", "release"]
