"""Live progress from a Codex TUI thread is broadcast only to its own session."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints import agent_control
from preloop.models import models
from preloop.models.crud import crud_runtime_session
from preloop.schemas.agent_control import AgentControlInboundEnvelope
from preloop.services.account_realtime import (
    ACCOUNT_REALTIME_TOPICS,
    ACCOUNT_TOPIC_SESSION_PROGRESS,
)


@pytest.fixture(autouse=True)
def _clear_target_cache() -> Iterator[None]:
    agent_control._session_progress_targets.clear()
    agent_control._session_progress_buckets.clear()
    yield
    agent_control._session_progress_targets.clear()
    agent_control._session_progress_buckets.clear()


class _FakeDatabase:
    """Stands in for ``_ControlDatabase``: runs the operation inline."""

    async def run(self, operation: Callable[[Any], Any]) -> Any:
        return operation(MagicMock())


def _connection(account_id: str, agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        account_id=account_id,
        managed_agent_id=agent_id,
        runtime_session_id=str(uuid4()),
        session_source_type="codex",
        session_source_id="sidecar",
    )


def _delta(source_id: str, **extra: Any) -> AgentControlInboundEnvelope:
    return AgentControlInboundEnvelope(
        type="event",
        name="session_progress",
        message_id="evt-1",
        payload={
            "kind": "assistant_delta",
            "session_source_id": source_id,
            "turn_id": "turn-1",
            "item_id": "m1",
            "delta": "Hello",
            **extra,
        },
    )


async def _emit(
    connection: SimpleNamespace,
    inbound: AgentControlInboundEnvelope,
    *,
    session: Any,
    owner_id: Any,
) -> MagicMock:
    with (
        patch.object(
            agent_control.crud_runtime_session,
            "get_open_by_source_id",
            return_value=session,
        ) as lookup,
        patch.object(
            agent_control,
            "session_managed_agent",
            return_value=SimpleNamespace(id=owner_id) if owner_id else None,
        ),
        patch.object(agent_control, "emit_account_event") as emit,
    ):
        await agent_control._emit_session_progress(_FakeDatabase(), connection, inbound)
    if session is not None or lookup.called:
        assert lookup.call_args.kwargs["account_id"] == connection.account_id
    return emit


@pytest.mark.asyncio
async def test_codex_assistant_delta_broadcast_scoped() -> None:
    account_id, agent_id = str(uuid4()), str(uuid4())
    connection = _connection(account_id, agent_id)
    session = SimpleNamespace(id=uuid4())
    emit = await _emit(
        connection,
        _delta("thread-tui", secret_extra="dropped"),
        session=session,
        owner_id=agent_id,
    )
    emit.assert_called_once()
    event = emit.call_args.args[0]
    assert event["account_id"] == account_id
    assert event["topic"] == ACCOUNT_TOPIC_SESSION_PROGRESS
    assert ACCOUNT_TOPIC_SESSION_PROGRESS in ACCOUNT_REALTIME_TOPICS
    assert event["type"] == "managed_agent_session_progress"
    assert event["runtime_session_id"] == str(session.id)
    assert event["session_source_id"] == "thread-tui"
    assert event["managed_agent_id"] == agent_id
    assert event["payload"]["kind"] == "assistant_delta"
    assert event["payload"]["delta"] == "Hello"
    assert "secret_extra" not in event["payload"]


@pytest.mark.asyncio
async def test_session_progress_for_another_agents_session_is_dropped() -> None:
    connection = _connection(str(uuid4()), str(uuid4()))
    emit = await _emit(
        connection,
        _delta("thread-of-someone-else"),
        session=SimpleNamespace(id=uuid4()),
        owner_id=str(uuid4()),
    )
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_session_progress_for_unknown_session_is_dropped() -> None:
    connection = _connection(str(uuid4()), str(uuid4()))
    emit = await _emit(connection, _delta("thread-x"), session=None, owner_id=None)
    emit.assert_not_called()


def test_session_progress_payload_allow_lists_and_bounds_fields() -> None:
    clean = agent_control._session_progress_payload(
        {
            "kind": "tool_end",
            "call_id": "call_1",
            "tool_name": "exec_command",
            "exit_code": 2,
            "duration_ms": 812,
            "status": "failed",
            "command": "x" * 1000,
            "api_key": "example-secret",
        }
    )
    assert clean is not None
    assert clean["exit_code"] == 2
    assert len(clean["command"]) == 400
    assert "api_key" not in clean
    assert agent_control._session_progress_payload({"kind": "rm -rf"}) is None
    assert agent_control._session_progress_payload({"kind": []}) is None
    assert agent_control._session_progress_payload({"kind": {}}) is None
    usage = agent_control._session_progress_payload(
        {"kind": "usage", "usage": {"input_tokens": 3, "output_tokens": True}}
    )
    assert usage == {"kind": "usage", "usage": {"input_tokens": 3}}


@pytest.mark.asyncio
async def test_generic_agent_control_topic_does_not_carry_session_progress() -> None:
    connection = _connection(str(uuid4()), str(uuid4()))
    with patch.object(agent_control, "emit_account_event") as emit:
        await agent_control._emit_agent_message(connection, _delta("thread-tui"))
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_session_progress_target_is_resolved_once_per_session() -> None:
    account_id, agent_id = str(uuid4()), str(uuid4())
    connection = _connection(account_id, agent_id)
    session = SimpleNamespace(id=uuid4())
    with (
        patch.object(
            agent_control.crud_runtime_session,
            "get_open_by_source_id",
            return_value=session,
        ) as lookup,
        patch.object(
            agent_control,
            "session_managed_agent",
            return_value=SimpleNamespace(id=agent_id),
        ),
        patch.object(agent_control, "emit_account_event") as emit,
    ):
        for _ in range(5):
            await agent_control._emit_session_progress(
                _FakeDatabase(), connection, _delta("thread-tui")
            )
        # Another agent never reuses this agent's cached mapping.
        other = _connection(account_id, str(uuid4()))
        await agent_control._emit_session_progress(
            _FakeDatabase(), other, _delta("thread-tui")
        )
    assert lookup.call_count == 2
    assert emit.call_count == 5


@pytest.mark.asyncio
async def test_reply_delta_published_only_to_owning_account_with_runtime_session_id() -> (
    None
):
    """A reply delta reaches the owning account's sockets, never another's."""
    from unittest.mock import AsyncMock

    from preloop.services.websocket_manager import WebSocketManager

    owner_account, other_account = str(uuid4()), str(uuid4())
    agent_id = str(uuid4())
    session = SimpleNamespace(id=uuid4())
    emit = await _emit(
        _connection(owner_account, agent_id),
        _delta("thread-tui", seq=7),
        session=session,
        owner_id=agent_id,
    )
    event = emit.call_args.args[0]
    assert event["account_id"] == owner_account
    assert event["runtime_session_id"] == str(session.id)
    assert event["payload"]["seq"] == 7

    manager = WebSocketManager()
    mine = MagicMock(send_text=AsyncMock())
    theirs = MagicMock(send_text=AsyncMock())
    mine_id = await manager.connect_with_account(mine, owner_account)
    theirs_id = await manager.connect_with_account(theirs, other_account)
    manager.subscribe(mine_id, ACCOUNT_TOPIC_SESSION_PROGRESS)
    manager.subscribe(theirs_id, ACCOUNT_TOPIC_SESSION_PROGRESS)
    # What the NATS consumer does with every ``account-updates.*`` message.
    await manager.broadcast_json(event, account_id=str(event["account_id"]))
    mine.send_text.assert_awaited_once()
    theirs.send_text.assert_not_awaited()


def test_session_progress_seq_is_a_bounded_non_negative_int() -> None:
    base = {"kind": "assistant_delta", "delta": "x"}
    assert agent_control._session_progress_payload({**base, "seq": 3})["seq"] == 3
    for bad in (-1, True, "4", 2**40):
        assert "seq" not in agent_control._session_progress_payload(
            {**base, "seq": bad}
        )


@pytest.mark.asyncio
async def test_chatty_reply_deltas_are_throttled_but_turn_bounds_are_not() -> None:
    account_id, agent_id = str(uuid4()), str(uuid4())
    connection = _connection(account_id, agent_id)
    session = SimpleNamespace(id=uuid4())
    with (
        patch.object(
            agent_control.crud_runtime_session,
            "get_open_by_source_id",
            return_value=session,
        ),
        patch.object(
            agent_control,
            "session_managed_agent",
            return_value=SimpleNamespace(id=agent_id),
        ),
        patch.object(agent_control, "emit_account_event") as emit,
    ):
        for index in range(100):
            await agent_control._emit_session_progress(
                _FakeDatabase(), connection, _delta("thread-tui", seq=index)
            )
        deltas = emit.call_count
        await agent_control._emit_session_progress(
            _FakeDatabase(),
            connection,
            _delta("thread-tui", kind="turn_completed", seq=100),
        )
    assert 0 < deltas <= agent_control._SESSION_PROGRESS_DELTA_BURST
    assert emit.call_count == deltas + 1
    assert emit.call_args.args[0]["payload"]["kind"] == "turn_completed"


def _runtime_session(
    db: Session,
    account_id: Any,
    source_id: str,
    *,
    age_minutes: int,
    ended: bool,
    source_type: str = "codex",
) -> models.RuntimeSession:
    now = datetime.now(UTC).replace(tzinfo=None)
    row = models.RuntimeSession(
        account_id=account_id,
        session_source_type=source_type,
        session_source_id=source_id,
        started_at=now - timedelta(minutes=age_minutes),
        ended_at=now if ended else None,
    )
    db.add(row)
    db.flush()
    return row


def test_get_open_by_source_id_scopes_account_and_skips_ended(
    db_session: Session,
) -> None:
    mine = models.Account(organization_name="Progress owner")
    theirs = models.Account(organization_name="Progress other")
    db_session.add_all([mine, theirs])
    db_session.flush()
    source_id = f"thread-{uuid4().hex[:8]}"
    # One row per source type: the source identity is unique per type.
    _runtime_session(
        db_session, mine.id, source_id, age_minutes=30, ended=False, source_type="a"
    )
    newest_open = _runtime_session(
        db_session, mine.id, source_id, age_minutes=5, ended=False, source_type="b"
    )
    _runtime_session(
        db_session, mine.id, source_id, age_minutes=1, ended=True, source_type="c"
    )
    foreign = _runtime_session(
        db_session, theirs.id, source_id, age_minutes=0, ended=False
    )

    found = crud_runtime_session.get_open_by_source_id(
        db_session, account_id=mine.id, session_source_id=source_id
    )
    assert found is not None and found.id == newest_open.id
    other = crud_runtime_session.get_open_by_source_id(
        db_session, account_id=theirs.id, session_source_id=source_id
    )
    assert other is not None and other.id == foreign.id
    assert (
        crud_runtime_session.get_open_by_source_id(
            db_session, account_id=mine.id, session_source_id="thread-unknown"
        )
        is None
    )
