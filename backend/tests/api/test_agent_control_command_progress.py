"""SDK-owned sessions announce command progress to the consuming session."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

from preloop.api.endpoints import agent_control
from preloop.models.crud import crud_runtime_session_activity


def test_command_progress_broadcast_scoped_by_runtime_session() -> None:
    owner_account = uuid4()
    consuming_account = uuid4()
    session_id = uuid4()
    context = SimpleNamespace(account_id=owner_account, managed_agent_id=uuid4())
    inbound = SimpleNamespace(
        type="event",
        name="command_progress",
        message_id="event-1",
        payload={
            "command_id": "command-1",
            "summary": "secret tool output text",
            "kind": "tool",
            "call_id": "call-1",
            "tool_name": "Bash",
        },
    )
    command = SimpleNamespace(
        kind="command", status="acked", consuming_account_id=consuming_account
    )
    events: list[dict[str, Any]] = []
    with (
        patch.object(agent_control, "crud_agent_control_command") as commands,
        patch.object(
            crud_runtime_session_activity,
            "log_agent_control_progress",
            return_value=SimpleNamespace(runtime_session_id=session_id),
        ),
        patch.object(agent_control, "emit_account_event", events.append),
    ):
        commands.get_by_command_id.return_value = command
        agent_control._persist_agent_control_progress(MagicMock(), context, inbound)

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "managed_agent_command_progress"
    # Its own topic: views that reload on any agent_control event stay quiet.
    assert event["topic"] == "session_progress"
    # The consuming account, never the agent owner's account.
    assert event["account_id"] == str(consuming_account)
    assert event["runtime_session_id"] == str(session_id)
    assert event["payload"]["runtime_session_id"] == str(session_id)
    assert event["payload"]["kind"] == "tool"
    assert event["payload"]["tool_name"] == "Bash"
    assert "secret tool output text" not in str(event)


def test_command_progress_without_session_is_not_broadcast() -> None:
    context = SimpleNamespace(account_id=uuid4(), managed_agent_id=uuid4())
    inbound = SimpleNamespace(
        type="event",
        name="command_progress",
        message_id="event-2",
        payload={"command_id": "command-2", "summary": "x", "kind": "assistant"},
    )
    command = SimpleNamespace(kind="command", status="acked", consuming_account_id=None)
    events: list[dict[str, Any]] = []
    with (
        patch.object(agent_control, "crud_agent_control_command") as commands,
        patch.object(
            crud_runtime_session_activity,
            "log_agent_control_progress",
            return_value=None,
        ),
        patch.object(agent_control, "emit_account_event", events.append),
    ):
        commands.get_by_command_id.return_value = command
        agent_control._persist_agent_control_progress(MagicMock(), context, inbound)
    assert events == []


def test_progress_chunks_nudge_embedding_only_after_the_frame_commits() -> None:
    """Chunks written with ``commit=False`` are handed to the worker post commit."""
    consuming_account = uuid4()
    order: list[str] = []
    with (
        patch.object(agent_control.control_connection, "authorize"),
        patch.object(agent_control, "_touch_presence"),
        patch.object(agent_control, "_mark_control_verified_from_capabilities"),
        patch.object(agent_control, "_handle_command_ack"),
        patch.object(agent_control, "_persist_agent_control_result", return_value=None),
        patch.object(
            agent_control,
            "_persist_agent_control_progress",
            return_value=consuming_account,
        ),
        patch.object(
            agent_control.control_connection,
            "commit",
            side_effect=lambda db: order.append("commit"),
        ),
        patch.object(
            agent_control,
            "request_embedding",
            side_effect=lambda account_id: order.append(f"nudge:{account_id}"),
        ),
    ):
        handled = agent_control._process_control_message(
            MagicMock(),
            MagicMock(),
            SimpleNamespace(payload={}),
            MagicMock(),
        )

    assert handled is True
    assert order == ["commit", f"nudge:{consuming_account}"]


def test_progress_persister_reports_the_consuming_account() -> None:
    """The account it returns is the one whose corpus received the chunks."""
    consuming_account = uuid4()
    context = SimpleNamespace(account_id=uuid4(), managed_agent_id=uuid4())
    inbound = SimpleNamespace(
        type="event",
        name="command_progress",
        message_id="event-2",
        payload={"command_id": "command-2", "summary": "step", "kind": "assistant"},
    )
    command = SimpleNamespace(
        kind="command", status="acked", consuming_account_id=consuming_account
    )
    with (
        patch.object(agent_control, "crud_agent_control_command") as commands,
        patch.object(
            crud_runtime_session_activity,
            "log_agent_control_progress",
            return_value=SimpleNamespace(runtime_session_id=uuid4()),
        ),
        patch.object(agent_control, "emit_account_event"),
    ):
        commands.get_by_command_id.return_value = command
        account = agent_control._persist_agent_control_progress(
            MagicMock(), context, inbound
        )

    assert account == consuming_account
