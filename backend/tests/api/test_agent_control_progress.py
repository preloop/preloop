"""Live progress from a Codex TUI thread is broadcast only to its own session."""

from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.api.endpoints import agent_control
from preloop.schemas.agent_control import AgentControlInboundEnvelope
from preloop.services.account_realtime import ACCOUNT_TOPIC_AGENT_CONTROL


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
    assert event["topic"] == ACCOUNT_TOPIC_AGENT_CONTROL
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
