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
        sessions.resolve_control_session.return_value = SimpleNamespace(id=session_id)
        activity.command_message.return_value = None
        agent_control._bind_command_session(MagicMock(), context, inbound)
        assert (
            sessions.resolve_control_session.call_args.kwargs["session_source_id"]
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


def test_idle_sidecar_session_resumes_after_reconnect() -> None:
    """An idle session remains resumable when the sidecar's map was lost."""
    agent = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        lifecycle_state="active",
        agent_kind="claude_code",
        display_name="Agent",
        runtime_session_id=uuid4(),
        session_source_type="claude_code",
        session_source_id="example-host",
        control_reachable_session_ids=[],
        control_last_heartbeat_at=None,
    )
    session = SimpleNamespace(
        id=uuid4(),
        ended_at=None,
        turn_state="your_turn",
        control_owned=True,
        session_source_type="claude_code",
        session_source_id="example-host:native-123",
        session_reference=None,
    )
    with (
        patch.object(dispatch, "session_managed_agent", return_value=agent),
        patch.object(dispatch, "agent_has_control_config", return_value=True),
        patch(
            "preloop.services.agent_control_presence.control_heartbeat_is_fresh",
            return_value=True,
        ),
    ):
        result = dispatch.resolve_session_control_mode(
            MagicMock(), account_id=str(agent.account_id), session=session
        )
    assert result.mode == "command"


def test_busy_sidecar_session_delivers_note_at_boundary() -> None:
    """Routing is based on the turn, not the console control used to send."""
    agent = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        lifecycle_state="active",
        agent_kind="claude_code",
        display_name="Agent",
        runtime_session_id=uuid4(),
        session_source_type="claude_code",
        session_source_id="example-host",
        control_reachable_session_ids=["native-123"],
        control_last_heartbeat_at=None,
    )
    session = SimpleNamespace(
        id=uuid4(),
        ended_at=None,
        turn_state="working",
        control_owned=True,
        session_source_type="claude_code",
        session_source_id="example-host:native-123",
        session_reference=None,
    )
    with (
        patch.object(dispatch, "session_managed_agent", return_value=agent),
        patch.object(dispatch, "agent_has_control_config", return_value=True),
        patch(
            "preloop.services.agent_control_presence.control_heartbeat_is_fresh",
            return_value=True,
        ),
    ):
        result = dispatch.resolve_session_control_mode(
            MagicMock(), account_id=str(agent.account_id), session=session
        )
    assert result.mode == "note"
    assert "next step" in result.reason


def test_start_directory_is_write_once() -> None:
    """Changing cwd during the turn must not change the resume directory."""
    from preloop.models.crud import crud_runtime_session

    session = SimpleNamespace(start_directory=None, cwd=None, control_owned=None)
    db = MagicMock()
    crud_runtime_session.record_start_directory(db, session=session, cwd="/tmp/project")
    session.cwd = "/tmp/elsewhere"
    crud_runtime_session.record_start_directory(
        db, session=session, cwd="/tmp/elsewhere"
    )
    assert session.start_directory == "/tmp/project"


def test_idle_note_endpoint_routes_as_a_resume() -> None:
    """An idle session cannot leave a follow-up waiting for a future hook."""
    from starlette.requests import Request
    from preloop.api.endpoints import operator_notes as endpoint
    from preloop.schemas.operator_notes import OperatorNoteCreate

    session_id = uuid4()
    agent = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4(), account_id=uuid4())
    command = SimpleNamespace(kind="command", command_id="resume-command")
    with (
        patch.object(endpoint, "ensure_permission_in_oss"),
        patch.object(endpoint, "_resolve_target", return_value=(agent.id, session_id)),
        patch.object(endpoint, "crud_runtime_session") as sessions,
        patch.object(
            dispatch,
            "resolve_session_control_mode",
            return_value=dispatch.SessionControlMode("command", None, None, agent),
        ),
        patch.object(
            endpoint, "_resume_note_target", create=True, return_value=command
        ) as resume,
        patch.object(endpoint, "crud_agent_control_command") as commands,
    ):
        sessions.get_account_session.return_value = SimpleNamespace(
            session_source_type="claude_code"
        )
        result = endpoint._create_note(
            MagicMock(),
            request=Request({"type": "http", "headers": []}),
            current_user=user,
            payload=OperatorNoteCreate(
                text="Continue the example", runtime_session_id=session_id
            ),
        )
    assert result is command
    resume.assert_called_once()
    commands.create_note.assert_not_called()


def test_resume_command_quotes_start_directory() -> None:
    """Commands are copyable even when a project contains spaces or quotes."""
    from preloop.schemas.gateway_usage import RuntimeSessionSummary

    session = RuntimeSessionSummary.model_construct(
        session_source_type="codex",
        session_source_id="example-host:native-123",
        session_reference=None,
        start_directory="/tmp/example's project",
        cwd="/tmp/other",
    )
    assert session.harness_session_id == "native-123"
    assert (
        session.resume_command
        == "cd '/tmp/example'\"'\"'s project' && codex resume native-123"
    )


def test_expired_command_error_does_not_write_into_session() -> None:
    """A late timeout frame cannot add output to an expired command."""
    command = SimpleNamespace(
        kind="command",
        status="expired",
        runtime_session_id=uuid4(),
        consuming_account_id=None,
    )
    context = SimpleNamespace(account_id=uuid4(), managed_agent_id=uuid4())
    inbound = SimpleNamespace(
        type="status",
        name="command_error",
        payload={"command_id": "expired-example", "error": "late failure"},
    )
    with (
        patch.object(agent_control, "crud_agent_control_command") as commands,
        patch.object(agent_control, "crud_runtime_session_activity") as activity,
    ):
        commands.get_by_command_id.return_value = command
        agent_control._persist_agent_control_result(MagicMock(), context, inbound)
    activity.log_agent_control_result.assert_not_called()
