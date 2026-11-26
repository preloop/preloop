"""Talk commands must retain the identity of their own harness conversation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.api.endpoints import agent_control
from preloop.services import agent_control_dispatch as dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["claude_code", "codex"])
async def test_new_command_never_uses_another_live_session(kind: str) -> None:
    """A machine's last session is not the conversation a new prompt creates."""
    agent = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        runtime_session_id=uuid4(),
        session_source_type=kind,
        session_source_id="example-host",
        agent_kind=kind,
    )
    with patch.object(
        dispatch, "persist_and_deliver_command", new_callable=AsyncMock
    ) as deliver:
        await dispatch.dispatch_operator_message(
            MagicMock(),
            managed_agent=agent,
            text="Example task",
            start_new_session=True,
        )
    assert deliver.call_args.kwargs["envelope"].runtime_session_id is None


def test_new_native_command_has_no_placeholder_history() -> None:
    """The session-start hook owns the one visible runtime identity."""
    agent = SimpleNamespace(
        account_id=uuid4(),
        session_source_type="claude_code",
        session_source_id="example-host",
        agent_kind="claude_code",
        display_name="Agent",
    )
    with patch.object(dispatch, "crud_runtime_session") as sessions:
        assert (
            dispatch.create_command_history_session(
                MagicMock(),
                agent=agent,
                start_new_session=True,
            )
            is None
        )
    sessions.upsert_by_source.assert_not_called()


def test_resume_identity_uses_native_suffix() -> None:
    """The gateway composite source key is not a harness resume id."""
    session = SimpleNamespace(
        session_source_type="claude_code",
        session_source_id="example-host:native-123",
        session_reference=None,
        start_directory="/tmp/example",
        cwd="/tmp/other",
    )
    identity = agent_control._existing_session_identity(session)
    assert identity["session_source_id"] == "native-123"
    assert identity["working_directory"] == "/tmp/example"


def test_composite_session_is_reachable_by_native_id() -> None:
    """Many native sessions can share one durable agent principal."""
    agent = SimpleNamespace(
        runtime_session_id=uuid4(),
        session_source_type="claude_code",
        session_source_id="example-host",
        control_reachable_session_ids=["native-123"],
    )
    session = SimpleNamespace(
        id=uuid4(),
        session_source_type="claude_code",
        session_source_id="example-host:native-123",
        session_reference=None,
    )
    assert dispatch.session_reachable_by_control(agent, session)


def test_native_start_binds_only_its_command() -> None:
    """A native hook id replaces no other live conversation's identity."""
    session_id = uuid4()
    context = SimpleNamespace(
        account_id=uuid4(),
        managed_agent_id=uuid4(),
        managed_agent_session_source_type="claude_code",
        managed_agent_session_source_id="example-host",
    )
    command = SimpleNamespace(
        kind="command",
        status="acked",
        runtime_session_id=None,
        consuming_account_id=None,
        envelope={"payload": {"text": "Example task"}},
        created_at=None,
    )
    inbound = SimpleNamespace(
        name="command_session",
        payload={
            "command_id": "example-command",
            "native_session_id": "native-123",
        },
    )
    with (
        patch.object(agent_control, "crud_agent_control_command") as commands,
        patch.object(agent_control, "crud_runtime_session") as sessions,
        patch.object(agent_control, "crud_runtime_session_activity") as activity,
        patch.object(agent_control, "emit_account_event"),
    ):
        commands.get_by_command_id.return_value = command
        sessions.upsert_by_source.return_value = SimpleNamespace(id=session_id)
        activity.command_message.return_value = None
        agent_control._bind_command_session(MagicMock(), context, inbound)
        assert (
            sessions.upsert_by_source.call_args.kwargs["session_source_id"]
            == "example-host:native-123"
        )
        assert (
            commands.bind_session.call_args.kwargs["runtime_session_id"] == session_id
        )
        assert (
            activity.log_agent_control_message.call_args.kwargs["runtime_session_id"]
            == session_id
        )
        commands.get_by_command_id.assert_called_once()
        assert (
            commands.get_by_command_id.call_args.kwargs["managed_agent_id"]
            == context.managed_agent_id
        )
