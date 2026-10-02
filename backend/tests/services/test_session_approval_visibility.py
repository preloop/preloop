"""Approval payloads remain permission gated even without topic subscriptions."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints.websockets import _set_approval_visibility
from preloop.services.websocket_manager import WebSocketManager


@pytest.mark.asyncio
@pytest.mark.parametrize("subscribed", [False, True])
async def test_approval_visibility_is_independent_of_subscription(
    subscribed: bool,
) -> None:
    manager = WebSocketManager()
    socket = MagicMock(send_text=AsyncMock())
    connection = await manager.connect_with_account(socket, "account-example")
    if subscribed:
        manager.subscribe(connection, "approvals")
    payload = {"type": "approval_created", "tool_args": {"path": "/workspace/example"}}
    await manager.broadcast_json(payload, account_id="account-example")
    socket.send_text.assert_not_awaited()
    manager.approval_visibility[connection] = True
    await manager.broadcast_json(payload, account_id="account-example")
    socket.send_text.assert_awaited_once()
    manager.disconnect(connection)
    assert connection not in manager.approval_visibility


@pytest.mark.asyncio
async def test_reauthentication_revokes_old_approval_visibility() -> None:
    manager = WebSocketManager()
    manager.approval_visibility["connection-example"] = True
    with (
        patch("preloop.api.endpoints.websockets.manager", manager),
        patch(
            "preloop.api.endpoints.websockets.run_db_async",
            new=AsyncMock(side_effect=HTTPException(403, "Permission denied")),
        ),
    ):
        await _set_approval_visibility("connection-example", MagicMock())
    assert manager.approval_visibility["connection-example"] is False
