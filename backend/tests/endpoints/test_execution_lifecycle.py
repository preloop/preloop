"""Bounded lifecycle stream and snapshot ordering, without external services."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import WebSocketDisconnect

from preloop.api.endpoints import websockets as ws
from preloop.services.execution_realtime import MAX_LIFECYCLE_BYTES, lifecycle_event


def socket() -> MagicMock:
    """Create an authenticated socket without live credentials or traffic."""
    user = SimpleNamespace(account_id=uuid4(), id=uuid4(), username="owner")
    result = MagicMock()
    result.scope = {"state": {"user": user}}
    result.accept = AsyncMock()
    result.close = AsyncMock()
    result.send_json = AsyncMock()
    result.send_text = AsyncMock()
    result.query_params = {}
    return result


def bus() -> MagicMock:
    """Mock NATS with a real async subscription boundary."""
    result = MagicMock()
    result.connect = AsyncMock()
    result.close = AsyncMock()
    result.nc.flush = AsyncMock()
    result.nc.subscribe = AsyncMock(return_value=MagicMock(unsubscribe=AsyncMock()))
    return result


@pytest.mark.asyncio
async def test_execution_socket_sends_snapshot_after_subscribe() -> None:
    client, event_bus = socket(), bus()
    execution_id = uuid4()
    order = []
    callback_task = None
    snapshot = {
        "type": "snapshot",
        "execution_id": str(execution_id),
        "seq": 4,
        "status": "STOPPED",
        "agent_session_reference": "run-1",
        "start_time": "2026-10-28T10:00:00+00:00",
        "end_time": "2026-10-28T10:01:00+00:00",
        "stop_requested_at": "2026-10-28T10:00:59+00:00",
        "stop_confirmed_at": "2026-10-28T10:01:00+00:00",
    }

    async def subscribe(subject: str, cb: object) -> MagicMock:
        nonlocal callback_task
        order.append("subscribed")
        callback_task = asyncio.create_task(
            cb(
                SimpleNamespace(
                    data=json.dumps(
                        {
                            "type": "status_update",
                            "execution_id": str(execution_id),
                            "seq": 5,
                            "payload": {"status": "STOPPED"},
                        }
                    ).encode()
                )
            )
        )
        await asyncio.sleep(0)
        return MagicMock(unsubscribe=AsyncMock())

    async def load(operation: object) -> object:
        if not order:
            return client.scope["state"]["user"].account_id
        order.append("snapshot_read")
        assert not callback_task.done()
        return snapshot

    async def receive() -> None:
        await callback_task
        raise WebSocketDisconnect()

    event_bus.nc.subscribe.side_effect = subscribe
    client.receive_json = receive
    with (
        patch.object(ws, "EventBus", return_value=event_bus),
        patch.object(ws, "run_db_async", load),
    ):
        await ws.flow_execution_websocket(client, execution_id)
    assert order == ["subscribed", "snapshot_read"]
    assert [call.args[0]["type"] for call in client.send_json.call_args_list] == [
        "connected",
        "snapshot",
        "status_update",
    ]
    assert client.send_json.call_args_list[1].args[0] == snapshot
    event_bus.nc.flush.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_flow_stream_filters_before_send_and_rechecks_permission(
    revoked: bool,
) -> None:
    client, event_bus = socket(), bus()
    flow_id = uuid4()
    account_id = client.scope["state"]["user"].account_id
    handler = None
    reads = AsyncMock(side_effect=[True, not revoked])

    async def subscribe(subject: str, cb: object, **kwargs: object) -> MagicMock:
        nonlocal handler
        handler = cb
        assert subject == "flow-updates.*"
        return MagicMock(unsubscribe=AsyncMock())

    async def receive() -> None:
        base = {
            "flow_id": str(flow_id),
            "account_id": str(account_id),
            "execution_id": str(uuid4()),
            "type": "status_update",
            "seq": 200,
            "lifecycle_seq": 2,
            "payload": {
                "status": "RUNNING",
                "line": "private" * 100000,
                "agent_session_reference": "😀" * 300000,
            },
        }
        for changes in (
            {"account_id": str(uuid4())},
            {"flow_id": str(uuid4())},
            {"account_id": None},
            {"type": "agent_log_line"},
            {},
        ):
            await handler(
                SimpleNamespace(data=json.dumps({**base, **changes}).encode())
            )
        raise WebSocketDisconnect()

    event_bus.nc.subscribe.side_effect = subscribe
    client.receive_text = receive
    with (
        patch.object(ws, "EventBus", return_value=event_bus),
        patch.object(ws, "run_db_async", reads),
    ):
        await ws.flow_lifecycle_websocket(client, flow_id)
    assert reads.await_count == 2  # wrong scope and log frames never query or send
    if revoked:
        client.send_text.assert_not_called()
        assert any(
            call.kwargs.get("code") == 1008 for call in client.close.call_args_list
        )
    else:
        client.send_text.assert_awaited_once()
        encoded = client.send_text.call_args.args[0]
        assert len(encoded.encode()) <= MAX_LIFECYCLE_BYTES
        event = json.loads(encoded)
        assert event["seq"] == 2
        assert event["sequence_domain"] == "lifecycle"
        assert "private" not in encoded
        assert "line" not in event["payload"]


@pytest.mark.asyncio
async def test_flow_stream_unauthorized_never_subscribes() -> None:
    client, event_bus = socket(), bus()
    with (
        patch.object(ws, "EventBus", return_value=event_bus),
        patch.object(ws, "run_db_async", AsyncMock(return_value=False)),
    ):
        await ws.flow_lifecycle_websocket(client, uuid4())
    event_bus.connect.assert_not_called()
    client.close.assert_awaited_once_with(code=1008)


def test_lifecycle_projection_uses_filtered_domain_and_withholds_unknown_fields() -> (
    None
):
    assert lifecycle_event({"type": "tool_call", "lifecycle_seq": 1}) is None
    event = lifecycle_event(
        {
            "type": "status_update",
            "seq": 700,
            "lifecycle_seq": 2,
            "payload": {"status": "RUNNING", "result": "secret"},
        }
    )
    assert event == {
        "type": "status_update",
        "seq": 2,
        "sequence_domain": "lifecycle",
        "payload": {"status": "RUNNING"},
    }


@pytest.mark.asyncio
async def test_runner_and_gateway_publishers_preserve_shared_cursor() -> None:
    """Both API-side publishers stamp exactly once before encoding."""
    from preloop.api.endpoints.runners import _publish_flow_update
    from preloop.services.model_gateway_events import ModelGatewayEventEmitter

    nc = MagicMock(publish=AsyncMock())
    execution_id = str(uuid4())
    runner = {"type": "agent_log_line", "payload": {"line": "synthetic"}}
    gateway = {
        "execution_id": execution_id,
        "type": "model_gateway_call",
        "payload": {},
    }
    with (
        patch(
            "preloop.sync.services.event_bus.get_nats_client",
            AsyncMock(return_value=nc),
        ),
        patch(
            "preloop.services.execution_realtime.stamp_execution_event_async",
            AsyncMock(side_effect=lambda e: {**e, "seq": 41}),
        ) as async_stamp,
        patch(
            "preloop.services.execution_realtime.stamp_execution_event",
            side_effect=lambda e: {**e, "seq": 42},
        ) as sync_stamp,
        patch("preloop.services.model_gateway_events.publish_realtime") as publish,
    ):
        await _publish_flow_update(execution_id, runner)
        ModelGatewayEventEmitter(MagicMock())._publish_to_nats(gateway)
    async_stamp.assert_awaited_once()
    sync_stamp.assert_called_once()
    assert json.loads(nc.publish.call_args.args[1])["seq"] == 41
    assert json.loads(publish.call_args.args[1])["seq"] == 42


@pytest.mark.asyncio
async def test_lifecycle_recovery_checks_permission_before_read() -> None:
    from fastapi import HTTPException

    async def off_loop(operation: object) -> object:
        return operation()

    with (
        patch.object(ws, "run_db_off_loop", off_loop),
        patch.object(ws, "ensure_permission_in_oss", side_effect=HTTPException(403)),
        patch.object(ws.crud_realtime, "lifecycle_snapshot") as read,
    ):
        with pytest.raises(HTTPException) as error:
            await ws.get_execution_lifecycle(
                uuid4(), socket().scope["state"]["user"], MagicMock()
            )
    assert error.value.status_code == 403
    read.assert_not_called()
