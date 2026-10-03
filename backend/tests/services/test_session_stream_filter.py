"""A session-attached socket only receives its session's events (#1149)."""

from typing import Any

import pytest

from preloop.services.websocket_manager import SessionStreamFilter, WebSocketManager

SESSION = "11c861cc-6b05-4fb3-a0ab-b8ea75f4d9be"
OTHER = "22222222-6b05-4fb3-a0ab-b8ea75f4d9be"
EXECUTION = "9f2b0000-0000-4000-8000-000000000002"


@pytest.mark.parametrize(
    ("event", "accepted"),
    [
        ({"topic": "runtime_sessions", "runtime_session_id": SESSION}, True),
        (
            {"topic": "gateway_activity", "payload": {"runtime_session_id": SESSION}},
            True,
        ),
        ({"topic": "runtime_sessions", "runtime_session_id": OTHER}, False),
        ({"topic": "flow_executions", "execution_id": EXECUTION}, False),
        ({"type": "approval_created", "runtime_session_id": SESSION}, False),
        ({"topic": "audit"}, False),
    ],
)
def test_session_only_filter(event: dict, accepted: bool) -> None:
    stream = SessionStreamFilter(runtime_session_id=SESSION)
    topic = WebSocketManager.resolve_topic(event)
    assert stream.accepts(event, topic) is accepted


def test_execution_and_visible_approvals_widen_the_filter() -> None:
    stream = SessionStreamFilter(
        runtime_session_id=SESSION, execution_id=EXECUTION, approvals_visible=True
    )
    for event in (
        {"topic": "flow_executions", "execution_id": EXECUTION},
        {"topic": "gateway_activity", "payload": {"flow_execution_id": EXECUTION}},
        {"type": "approval_created", "runtime_session_id": SESSION},
        {"type": "approval_approved", "execution_id": EXECUTION},
    ):
        assert stream.accepts(event, WebSocketManager.resolve_topic(event)), event
    other = {"type": "approval_created", "runtime_session_id": OTHER}
    assert not stream.accepts(other, WebSocketManager.resolve_topic(other))


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


@pytest.mark.asyncio
async def test_broadcast_applies_the_filter_only_to_attached_sockets() -> None:
    manager = WebSocketManager()
    console, attached, foreign = _Socket(), _Socket(), _Socket()
    for name, socket, account in (
        ("console", console, "acct"),
        ("attached", attached, "acct"),
        ("foreign", foreign, "other"),
    ):
        manager.active_connections[name] = socket  # type: ignore[assignment]
        manager.connection_accounts[name] = account
    manager.session_streams["attached"] = SessionStreamFilter(
        runtime_session_id=SESSION
    )

    mine: dict[str, Any] = {"topic": "runtime_sessions", "runtime_session_id": SESSION}
    theirs: dict[str, Any] = {"topic": "runtime_sessions", "runtime_session_id": OTHER}
    await manager.broadcast_json(mine, account_id="acct")
    await manager.broadcast_json(theirs, account_id="acct")

    assert len(console.sent) == 2
    assert len(attached.sent) == 1 and SESSION in attached.sent[0]
    assert foreign.sent == []

    manager.disconnect("attached")
    assert "attached" not in manager.session_streams


def test_unsubscribing_a_topic_keeps_the_session_filter() -> None:
    manager = WebSocketManager()
    manager.active_connections["attached"] = _Socket()  # type: ignore[assignment]
    manager.session_streams["attached"] = SessionStreamFilter(
        runtime_session_id=SESSION
    )
    assert manager.subscribe("attached", "runtime_sessions")

    assert manager.unsubscribe("attached", "runtime_sessions")

    assert "attached" in manager.session_streams
