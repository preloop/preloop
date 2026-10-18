"""The ``presence`` and ``activity_metrics`` realtime topics (#1889).

Person presence is gated like approvals: a connection with ``view_users``
gets everyone's, any other connection only its own user's. Metrics are
account aggregates and reach every connection on the account.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from preloop.services.account_realtime import (
    ACCOUNT_REALTIME_TOPICS,
    ACCOUNT_TOPIC_ACTIVITY_METRICS,
    ACCOUNT_TOPIC_PRESENCE,
    build_account_event,
)
from preloop.services.websocket_manager import WebSocketManager

pytestmark = pytest.mark.asyncio

ACCOUNT = "acct-1"


def _manager() -> tuple[WebSocketManager, dict[str, AsyncMock]]:
    manager = WebSocketManager()
    sockets: dict[str, AsyncMock] = {}
    for conn, account, user, admin in (
        ("admin", ACCOUNT, "u-admin", True),
        ("member", ACCOUNT, "u-member", False),
        ("other-account", "acct-2", "u-x", True),
    ):
        socket = AsyncMock()
        sockets[conn] = socket
        manager.active_connections[conn] = socket
        manager.connection_accounts[conn] = account
        manager.connection_users[conn] = user
        manager.presence_visibility[conn] = admin
    return manager, sockets


def _received(sockets: dict[str, AsyncMock]) -> set[str]:
    return {name for name, socket in sockets.items() if socket.send_text.await_count}


def _presence(subject_type: str, subject_id: str) -> dict[str, Any]:
    return build_account_event(
        account_id=ACCOUNT,
        topic=ACCOUNT_TOPIC_PRESENCE,
        event_type="presence_changed",
        payload={"subject_type": subject_type, "subject_id": subject_id},
    )


def test_topics_are_registered() -> None:
    assert ACCOUNT_TOPIC_PRESENCE in ACCOUNT_REALTIME_TOPICS
    assert ACCOUNT_TOPIC_ACTIVITY_METRICS in ACCOUNT_REALTIME_TOPICS


async def test_person_presence_reaches_admins_and_the_subject_only() -> None:
    manager, sockets = _manager()
    await manager.broadcast_json(_presence("person", "u-somebody"), ACCOUNT)
    assert _received(sockets) == {"admin"}

    manager, sockets = _manager()
    await manager.broadcast_json(_presence("person", "u-member"), ACCOUNT)
    assert _received(sockets) == {"admin", "member"}


async def test_person_device_presence_honours_the_own_row_exception() -> None:
    manager, sockets = _manager()
    await manager.broadcast_json(_presence("person_device", "u-member:mbp"), ACCOUNT)
    assert _received(sockets) == {"admin", "member"}
    manager, sockets = _manager()
    await manager.broadcast_json(_presence("device", "mbp"), ACCOUNT)
    assert _received(sockets) == {"admin"}


async def test_agent_presence_and_metrics_reach_the_whole_account() -> None:
    manager, sockets = _manager()
    await manager.broadcast_json(_presence("agent", "agent-1"), ACCOUNT)
    assert _received(sockets) == {"admin", "member"}

    manager, sockets = _manager()
    metrics = build_account_event(
        account_id=ACCOUNT,
        topic=ACCOUNT_TOPIC_ACTIVITY_METRICS,
        event_type="activity_metrics_updated",
        payload={"active_people": 2},
    )
    await manager.broadcast_json(metrics, ACCOUNT)
    assert _received(sockets) == {"admin", "member"}
    sent = json.loads(sockets["member"].send_text.await_args.args[0])
    assert sent["payload"] == {"active_people": 2}


async def test_unknown_visibility_fails_closed() -> None:
    manager, sockets = _manager()
    manager.presence_visibility.pop("admin")
    manager.connection_users.pop("admin")
    await manager.broadcast_json(_presence("person", "u-admin"), ACCOUNT)
    assert _received(sockets) == set()


async def test_disconnect_forgets_presence_state() -> None:
    manager, _ = _manager()
    manager.disconnect("member")
    assert "member" not in manager.presence_visibility
    assert "member" not in manager.connection_users


async def test_socket_authentication_records_presence_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from preloop.api.endpoints import websockets as endpoint

    manager = WebSocketManager()
    monkeypatch.setattr(endpoint, "manager", manager)
    user = SimpleNamespace(id="u-1")

    async def allowed(check: Any) -> bool:
        return True

    async def denied(check: Any) -> bool:
        raise HTTPException(status_code=403)

    monkeypatch.setattr(endpoint, "run_db_async", allowed)
    await endpoint._set_presence_visibility("c1", user)  # type: ignore[arg-type]
    assert manager.presence_visibility["c1"] is True
    assert manager.connection_users["c1"] == "u-1"

    monkeypatch.setattr(endpoint, "run_db_async", denied)
    await endpoint._set_presence_visibility("c2", user)  # type: ignore[arg-type]
    assert manager.presence_visibility["c2"] is False
    assert manager.connection_users["c2"] == "u-1"
