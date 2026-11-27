"""Native Talk routing through the HTTP endpoints and a real database."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from preloop.models import models
from preloop.models.crud import (
    crud_runtime_session,
    crud_agent_control_command,
    crud_managed_agent_enrollment,
)
from preloop.services.agent_session_lineage import register_session_start
from tests.endpoints.test_agent_control_persistence import _setup_controllable_agent


def native_session(
    client: TestClient, db: Session, user: models.User
) -> tuple[Any, Any]:
    """Create an enrolled host and one native sidecar conversation."""
    agent, _ = _setup_controllable_agent(
        client, db, user, session_source_id="example-host"
    )
    agent.agent_kind = "claude_code"
    agent.session_source_type = "claude_code"
    agent.control_last_heartbeat_at = datetime.now(UTC)
    agent.control_reachable_session_ids = []
    agent.control_connection_id = uuid4()
    enrollment = crud_managed_agent_enrollment.get_latest_for_agent_by_type(
        db,
        account_id=user.account_id,
        agent_id=agent.id,
        enrollment_type="runtime_plugin_control",
    )
    if enrollment is None:
        enrollment = crud_managed_agent_enrollment.get_latest_for_agent_by_type(
            db,
            account_id=user.account_id,
            agent_id=agent.id,
            enrollment_type="cli_managed_config",
        )
    enrollment.validation_result = {
        **enrollment.validation_result,
        "talk_connection_id": str(agent.control_connection_id),
        "talk_capabilities": {
            "start_directory": True,
            "command_session": True,
            "followup_prompts": True,
        },
    }
    db.commit()
    result = register_session_start(
        db,
        account_id=user.account_id,
        principal_type="claude_code",
        principal_id=agent.session_source_id,
        principal_name="Example agent",
        external_session_id="example-native",
        cwd="/tmp/example project",
        control_owned=True,
    )
    session = crud_runtime_session.get_account_session(
        db, account_id=user.account_id, runtime_session_id=result.runtime_session_id
    )
    session.turn_state = "your_turn"
    db.commit()
    return agent, session


@pytest.mark.parametrize("endpoint", ["note", "prompt"])
def test_idle_input_resumes_exact_native_session(
    client: TestClient, db_session: Session, test_user: models.User, endpoint: str
) -> None:
    agent, session = native_session(client, db_session, test_user)
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ) as send:
        if endpoint == "note":
            response = client.post(
                "/api/v1/operator-notes",
                json={
                    "text": "Continue the example",
                    "runtime_session_id": str(session.id),
                },
            )
        else:
            response = client.post(
                f"/api/v1/agents/{agent.id}/control/prompts",
                json={
                    "message": "Continue the example",
                    "target_session_id": str(session.id),
                },
            )
    assert response.status_code in {201, 202}, response.text
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=test_user.account_id,
        command_id=response.json()["command_id"],
    )
    assert command.kind == "command"
    assert command.runtime_session_id == session.id
    payload = send.call_args.kwargs["envelope"].payload
    assert payload["session_source_id"] == "example-native"
    assert payload["working_directory"] == "/tmp/example project"


@pytest.mark.parametrize("state,owned", [("working", True), ("your_turn", False)])
def test_busy_owned_prompt_and_interactive_note(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    state: str,
    owned: bool,
) -> None:
    agent, session = native_session(client, db_session, test_user)
    session.turn_state, session.control_owned = state, owned
    db_session.commit()
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ) as send:
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "Example follow-up", "target_session_id": str(session.id)},
        )
    assert response.status_code == 202, response.text
    if owned:
        assert response.json()["note_id"] is None
        send.assert_called_once()
    else:
        assert response.json()["note_id"]
        send.assert_not_called()


def test_offline_idle_note_refuses_with_local_resume_command(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent, session = native_session(client, db_session, test_user)
    agent.control_last_heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    response = client.post(
        "/api/v1/operator-notes",
        json={"text": "Continue", "runtime_session_id": str(session.id)},
    )
    assert response.status_code == 409, response.text
    assert (
        response.json()["detail"]["resume_command"]
        == "cd '/tmp/example project' && claude --resume example-native"
    )


def test_resume_cancels_only_pending_notes(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent, session = native_session(client, db_session, test_user)
    notes = []
    for state in ("pending", "delivered"):
        note = crud_agent_control_command.create_note(
            db_session,
            account_id=test_user.account_id,
            managed_agent_id=agent.id,
            runtime_session_id=session.id,
            note_id=str(uuid4()),
            body="Earlier example",
            envelope={},
            author_display="Jane Doe",
            author_auth_method="session",
            created_by_user_id=test_user.id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        note.status = state
        db_session.commit()
        notes.append(note)
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = client.post(
            "/api/v1/operator-notes",
            json={"text": "Resume the example", "runtime_session_id": str(session.id)},
        )
    assert response.status_code == 201, response.text
    for note in notes:
        db_session.refresh(note)
    assert [note.status for note in notes] == ["cancelled", "delivered"]


def test_new_command_binds_hook_session_among_other_live_sessions(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
) -> None:
    """The hook, command, progress and result share one native conversation."""
    from types import SimpleNamespace
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlInboundEnvelope

    agent, unrelated = native_session(client, db_session, test_user)
    with patch.object(
        agent_control.agent_control_manager,
        "send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={
                "message": "A separate example",
                "start_new_session": True,
                "working_directory": "/tmp/second-example",
            },
        )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["runtime_session_id"] is None
    assert body["target_session_id"] is None
    command = crud_agent_control_command.get_by_command_id(
        db_session, account_id=test_user.account_id, command_id=body["command_id"]
    )
    assert command.runtime_session_id is None
    hook = register_session_start(
        db_session,
        account_id=test_user.account_id,
        principal_type="claude_code",
        principal_id=agent.session_source_id,
        principal_name="Example agent",
        external_session_id="second-native",
        cwd="/tmp/second-example",
        control_owned=True,
    )
    crud_agent_control_command.mark_delivered(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        command_id=command.command_id,
        delivered_at=datetime.now(UTC),
    )
    context = SimpleNamespace(
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        managed_agent_session_source_type="claude_code",
        managed_agent_session_source_id=agent.session_source_id,
    )
    inbound = AgentControlInboundEnvelope(
        type="event",
        name="command_session",
        payload={
            "command_id": command.command_id,
            "native_session_id": "second-native",
            "working_directory": "/tmp/second-example",
        },
    )
    with patch.object(agent_control, "emit_account_event"):
        agent_control._bind_command_session(db_session, context, inbound)
    db_session.refresh(command)
    assert str(command.runtime_session_id) == hook.runtime_session_id
    assert command.runtime_session_id != unrelated.id
    assert agent_control._resolve_command_progress_target(
        db_session, context, command.command_id
    ) == (str(test_user.account_id), hook.runtime_session_id)
    bound = crud_runtime_session.get_account_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=hook.runtime_session_id,
    )
    assert bound.start_directory == "/tmp/second-example"
    assert bound.control_owned is True


def test_polling_a_queued_expired_command_is_terminal(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """An offline sidecar is not needed to expire a queued prompt."""
    from tests.endpoints.test_agent_control_persistence import _persist_command

    agent, _ = native_session(client, db_session, test_user)
    record = _persist_command(
        db_session,
        test_user,
        agent,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    response = client.get(
        f"/api/v1/agents/{agent.id}/control/commands/{record.command_id}"
    )
    assert response.status_code == 200
    assert response.json()["delivery_state"] == "expired"
    assert response.json()["terminal"] is True


@pytest.mark.parametrize("reopened", [False, True])
def test_new_command_rejects_a_preexisting_native_identity(
    client: TestClient, db_session: Session, test_user: models.User, reopened: bool
) -> None:
    """Even an erroneous sidecar startup frame cannot reassign old history."""
    from types import SimpleNamespace
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlInboundEnvelope

    agent, existing = native_session(client, db_session, test_user)
    with patch.object(
        agent_control.agent_control_manager,
        "send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "Separate example", "start_new_session": True},
        )
    command_id = response.json()["command_id"]
    if reopened:
        existing.created_at = datetime.now(UTC) - timedelta(days=1)
        existing.started_at = datetime.now(UTC)
        db_session.commit()
    crud_agent_control_command.mark_delivered(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        command_id=command_id,
        delivered_at=datetime.now(UTC),
    )
    context = SimpleNamespace(
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        managed_agent_session_source_type="claude_code",
        managed_agent_session_source_id=agent.session_source_id,
    )
    inbound = AgentControlInboundEnvelope(
        type="event",
        name="command_session",
        payload={"command_id": command_id, "native_session_id": "example-native"},
    )
    with patch.object(agent_control, "emit_account_event"):
        agent_control._bind_command_session(db_session, context, inbound)
    command = crud_agent_control_command.get_by_command_id(
        db_session, account_id=test_user.account_id, command_id=command_id
    )
    assert command.runtime_session_id is None
    assert command.status == "failed"


@pytest.mark.parametrize("endpoint", ["note", "prompt"])
def test_resume_refuses_unknown_start_directory(
    client: TestClient, db_session: Session, test_user: models.User, endpoint: str
) -> None:
    """A last-seen cwd cannot substitute for the original launch directory."""
    agent, session = native_session(client, db_session, test_user)
    session.start_directory = None
    session.cwd = "/tmp/changed-directory"
    db_session.commit()
    if endpoint == "note":
        response = client.post(
            "/api/v1/operator-notes",
            json={"text": "Continue", "runtime_session_id": str(session.id)},
        )
    else:
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "Continue", "target_session_id": str(session.id)},
        )
    assert response.status_code == 409, response.text
    assert "original working directory" in response.json()["detail"]["message"]


@pytest.mark.parametrize("endpoint", ["note", "prompt"])
def test_stale_unknown_session_does_not_queue_forever(
    client: TestClient, db_session: Session, test_user: models.User, endpoint: str
) -> None:
    """Legacy sessions with no owner evidence must not strand new messages."""
    agent, session = native_session(client, db_session, test_user)
    session.control_owned = None
    session.last_activity_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    if endpoint == "note":
        response = client.post(
            "/api/v1/operator-notes",
            json={"text": "Continue", "runtime_session_id": str(session.id)},
        )
    else:
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "Continue", "target_session_id": str(session.id)},
        )
    assert response.status_code == 409, response.text
    assert "confirm" in response.json()["detail"]["message"]


def _pending_note(db: Session, user: models.User, agent: Any, session: Any) -> Any:
    note = crud_agent_control_command.create_note(
        db,
        account_id=user.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        note_id=str(uuid4()),
        body="Earlier example",
        envelope={},
        author_display="Jane Doe",
        author_auth_method="session",
        created_by_user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.commit()
    return note


def test_failed_prompt_delivery_keeps_pending_notes(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent, session = native_session(client, db_session, test_user)
    note = _pending_note(db_session, test_user, agent, session)
    with (
        patch(
            "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "preloop.api.endpoints.agent_control._publish_command",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "Resume", "target_session_id": str(session.id)},
        )
    assert response.status_code == 503, response.text
    db_session.refresh(note)
    assert note.status == "pending"


def test_prompt_note_does_not_need_a_live_binding(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent, session = native_session(client, db_session, test_user)
    agent.runtime_session_id = None
    session.turn_state = "working"
    session.control_owned = False
    db_session.commit()
    response = client.post(
        f"/api/v1/agents/{agent.id}/control/prompts",
        json={"message": "Example follow-up", "target_session_id": str(session.id)},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["note_id"]
    assert body["command_envelope"]["runtime_session_id"] == str(session.id)


def test_non_native_terminal_session_is_refused(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    agent, _ = native_session(client, db_session, test_user)
    agent.agent_kind = agent.session_source_type = "hermes"
    db_session.commit()
    result = register_session_start(
        db_session,
        account_id=test_user.account_id,
        principal_type="hermes",
        principal_id=agent.session_source_id,
        principal_name="Example agent",
        external_session_id="example-terminal",
        cwd="/tmp/example",
        control_owned=False,
    )
    session = crud_runtime_session.get_account_session(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=result.runtime_session_id,
    )
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
    ) as send:
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "Example", "target_session_id": str(session.id)},
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "terminal_session"
    send.assert_not_called()


def test_old_sidecar_start_directory_is_refused(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """A replaced old process must not start in an ignored directory."""
    agent, _ = native_session(client, db_session, test_user)
    agent.control_connection_id = uuid4()
    db_session.commit()
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
    ) as send:
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={
                "message": "Example task",
                "start_new_session": True,
                "working_directory": "/tmp/example",
            },
        )
    assert response.status_code == 409
    assert "working directories" in response.text
    send.assert_not_called()


def test_start_options_exposes_current_sidecar_features(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """The phone picker reads capability and directory choices before a send."""
    agent, _ = native_session(client, db_session, test_user)
    response = client.get(f"/api/v1/agents/{agent.id}/control/start-options")
    assert response.status_code == 200, response.text
    options = response.json()
    assert options["capabilities"]["start_directory"] is True
    assert options["recent_directories"] == ["/tmp/example project"]
    assert options["projects"] == []


def test_prompt_delivery_receipts_update_the_transcript(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Socket acknowledgement does not pretend a queued SDK prompt has run."""
    from preloop.models.crud.talk_delivery import record_prompt_delivery
    from preloop.models.crud import crud_runtime_session_activity

    agent, session = native_session(client, db_session, test_user)
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts",
            json={"message": "Example follow-up", "target_session_id": str(session.id)},
        )
    assert response.status_code == 202
    command_id = response.json()["command_id"]
    for state in ("queued", "sent"):
        record_prompt_delivery(
            db_session,
            account_id=test_user.account_id,
            agent_id=agent.id,
            command_id=command_id,
            state=state,
        )
        db_session.commit()
        status = client.get(f"/api/v1/agents/{agent.id}/control/commands/{command_id}")
        assert status.json()["prompt_delivery"] == state
        activity = crud_runtime_session_activity.command_message(
            db_session, account_id=test_user.account_id, command_id=command_id
        )
        assert activity.metadata_["prompt_delivery"] == state


def test_one_result_does_not_end_another_queued_prompt(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Concurrent prompt receipts keep the conversation working until all settle."""
    agent, session = native_session(client, db_session, test_user)
    commands = []
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ):
        for text in ("Example first prompt", "Example second prompt"):
            response = client.post(
                f"/api/v1/agents/{agent.id}/control/prompts",
                json={"message": text, "target_session_id": str(session.id)},
            )
            assert response.status_code == 202
            command = crud_agent_control_command.get_by_command_id(
                db_session,
                account_id=test_user.account_id,
                command_id=response.json()["command_id"],
            )
            command.status = "acked"
            commands.append(command)
    session.turn_state = "working"
    db_session.commit()
    for index, command in enumerate(commands):
        crud_agent_control_command.mark_terminal_result(
            db_session,
            account_id=test_user.account_id,
            managed_agent_id=agent.id,
            command_id=command.command_id,
            result_payload={"status": "completed"},
        )
        crud_runtime_session.record_control_turn(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            completed=True,
        )
        assert session.turn_state == ("working" if index == 0 else "your_turn")


@pytest.mark.parametrize("targeted", [True, False])
def test_old_sidecar_interrupt_does_not_require_prompt_features(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    targeted: bool,
) -> None:
    """An explicit interrupt keeps working before a prompt-capable update."""
    agent, session = native_session(client, db_session, test_user)
    agent.control_connection_id = uuid4()
    db_session.commit()
    payload: dict[str, Any] = {"message": "Interrupt", "interrupt": True}
    if targeted:
        payload["target_session_id"] = str(session.id)
    with patch(
        "preloop.api.endpoints.agent_control.agent_control_manager.send_to_agent",
        new_callable=AsyncMock,
        return_value=True,
    ) as send:
        response = client.post(
            f"/api/v1/agents/{agent.id}/control/prompts", json=payload
        )
    assert response.status_code == 202, response.text
    assert send.call_args.kwargs["envelope"].payload["interrupt"] is True
