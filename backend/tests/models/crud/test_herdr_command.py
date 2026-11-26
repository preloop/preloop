"""Talk's herdr rung stays account/device scoped and never reaches a sidecar."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_agent_control_command
from preloop.models.crud import herdr_command
from preloop.services import herdr_commands
from preloop.services.agent_control_dispatch import resolve_session_control_mode
from preloop.services.herdr_inventory import record_herdr_inventory
from tests.models.crud.test_terminal_location import terminal_data  # noqa: F401


@pytest.fixture
def prompt_data(
    db_session: Session,
    terminal_data: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    data = terminal_data
    runner = data["runner"]
    runner.status = "online"
    runner.last_heartbeat = datetime.now(UTC)
    data["report"]["prompt_enabled"] = True
    data["report"]["agents"][0]["state"] = "idle"
    record_herdr_inventory(db_session, runner, data["report"])
    session = data["rows"][0]
    agent = (
        db_session.query(models.ManagedAgent)
        .filter(
            models.ManagedAgent.account_id == runner.account_id,
            models.ManagedAgent.session_source_id == session.runtime_principal_id,
        )
        .one()
    )
    monkeypatch.setattr(
        "preloop.services.agent_control_dispatch.agent_has_control_config",
        lambda *args, **kwargs: False,
    )
    data.update(session=session, agent=agent)
    return data


def _dispatch(db: Session, data: dict[str, Any]) -> Any:
    decision = resolve_session_control_mode(
        db, account_id=str(data["runner"].account_id), session=data["session"]
    )
    assert decision.mode == "command" and decision.herdr_target
    return herdr_commands.dispatch_prompt(
        db,
        agent=data["agent"],
        session=data["session"],
        target=decision.herdr_target,
        text="Example\nnext step",
        user_id=data["runner"].registered_by_user_id,
    )


def test_queue_replay_and_result_are_audited_and_bound(
    db_session: Session, prompt_data: dict[str, Any]
) -> None:
    data = prompt_data
    dispatched = _dispatch(db_session, data)
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    assert command.envelope["payload"]["delivery_route"] == "herdr_pane"
    assert command.runtime_session_id == data["session"].id
    assert command.created_by_user_id == data["runner"].registered_by_user_id
    assert not crud_agent_control_command.get_undelivered_for_agent(
        db_session,
        account_id=data["runner"].account_id,
        managed_agent_id=data["agent"].id,
        now=datetime.now(UTC),
    )
    frames = herdr_commands.pending_messages(db_session, data["runner"])
    assert frames[0]["text"] == "Examplenext step"
    assert frames == herdr_commands.pending_messages(db_session, data["runner"])
    assert frames[0]["command_id"] == dispatched.command_id
    wrong = models.FlowRunner(
        account_id=data["runner"].account_id,
        name="Other",
        token_hash="example",
        workstation_fingerprint=data["runner"].workstation_fingerprint,
    )
    db_session.add(wrong)
    db_session.flush()
    result = {
        "command_id": dispatched.command_id,
        "code": "delivered",
        "delivery": "sent",
        "reason": "untrusted output",
        "state": "idle",
    }
    assert not herdr_commands.apply_result(db_session, wrong, result)
    assert herdr_commands.apply_result(db_session, data["runner"], result)
    assert command.status == "acked"
    payload = crud_agent_control_command.command_result_payload(command)
    assert payload["delivery"] == "sent" and payload["reason"] != "untrusted output"
    assert not herdr_commands.apply_result(
        db_session, data["runner"], dict(result, delivery="unknown", code="timeout")
    )
    assert herdr_commands.pending_messages(db_session, data["runner"]) == []


@pytest.mark.parametrize(
    "guard",
    [
        "disabled",
        "blocked",
        "closed",
        "other_device",
        "other_kind",
        "other_native",
        "ambiguous_runner",
        "working",
        "unknown_protocol",
    ],
)
def test_route_and_dispatch_fail_closed(
    db_session: Session, prompt_data: dict[str, Any], guard: str
) -> None:
    data = prompt_data
    target = herdr_command.eligible_target(
        db_session,
        account_id=data["runner"].account_id,
        session=data["session"],
        agent=data["agent"],
    )
    assert target
    location = dict(data["session"].terminal_location)
    report = dict(data["runner"].harness_inventory["herdr"])
    if guard == "disabled":
        report["prompt_enabled"] = False
    if guard == "blocked":
        location["state"] = "blocked"
    if guard == "closed":
        location["pane_closed_at"] = datetime.now(UTC).isoformat()
    if guard == "other_device":
        location["device_id"] = "b" * 64
    if guard == "other_kind":
        data["session"].runtime_principal_type = "opencode"
    if guard == "other_native":
        data["session"].session_reference = "different-native"
    if guard == "working":
        data["session"].turn_state = "working"
    if guard == "unknown_protocol":
        report["protocol"] = 99
    if guard == "ambiguous_runner":
        db_session.add(
            models.FlowRunner(
                account_id=data["runner"].account_id,
                name="Duplicate",
                token_hash="example-duplicate",
                workstation_fingerprint=data["runner"].workstation_fingerprint,
                status="online",
                last_heartbeat=datetime.now(UTC),
            )
        )
    data["session"].terminal_location = location
    data["runner"].harness_inventory = data["runner"].harness_inventory | {
        "herdr": report
    }
    db_session.flush()
    from preloop.services.agent_control_dispatch import AgentControlDispatchError

    with pytest.raises(AgentControlDispatchError):
        herdr_commands.dispatch_prompt(
            db_session,
            agent=data["agent"],
            session=data["session"],
            target=target,
            text="Example",
            user_id=data["runner"].registered_by_user_id,
        )
    assert db_session.query(models.AgentControlCommand).count() == 0


@pytest.mark.parametrize("delivered", [False, True])
def test_abandoned_commands_expire_without_online_runner(
    db_session: Session, prompt_data: dict[str, Any], delivered: bool
) -> None:
    data = prompt_data
    dispatched = _dispatch(db_session, data)
    if delivered:
        herdr_commands.pending_messages(db_session, data["runner"])
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    command.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()
    assert herdr_commands.expire_commands(db_session) == 1
    assert command.status == "expired"
    assert crud_agent_control_command.command_result_payload(command)["delivery"] == (
        "unknown" if delivered else "not_sent"
    )
    assert herdr_commands.expire_commands(db_session) == 0


@pytest.mark.parametrize(
    "code,delivery",
    [
        ("agent_blocked", "not_sent"),
        ("agent_prompt_stalled", "unknown"),
        ("timeout", "unknown"),
        ("pane_gone", "not_sent"),
        ("prompt_disabled", "not_sent"),
        ("session_changed", "not_sent"),
        ("command_expired", "not_sent"),
    ],
)
def test_refusal_results_end_commands(
    db_session: Session, prompt_data: dict[str, Any], code: str, delivery: str
) -> None:
    data = prompt_data
    dispatched = _dispatch(db_session, data)
    herdr_commands.pending_messages(db_session, data["runner"])
    assert herdr_commands.apply_result(
        db_session,
        data["runner"],
        {"command_id": dispatched.command_id, "code": code, "delivery": delivery},
    )
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    assert command.status in {"failed", "expired"}
    assert command.last_error


@pytest.mark.asyncio
async def test_existing_prompt_endpoint_uses_same_consent_and_history(
    db_session: Session, prompt_data: dict[str, Any], test_user: models.User
) -> None:
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlSendMessageRequest

    data = prompt_data
    with (
        patch.object(
            agent_control, "_require_session_start_consent", new_callable=AsyncMock
        ) as consent,
        patch.object(
            agent_control, "dispatch_operator_message", new_callable=AsyncMock
        ) as sidecar,
    ):
        response = await agent_control._route_managed_agent_prompt(
            db=db_session,
            agent_id=str(data["agent"].id),
            current_user=test_user,
            request=AgentControlSendMessageRequest(
                message="Example prompt",
                target_session_id=data["session"].id,
                metadata={"source": "cli"},
            ),
        )
    consent.assert_awaited_once()
    sidecar.assert_not_awaited()
    command = crud_agent_control_command.get_by_command_id(
        db_session, account_id=test_user.account_id, command_id=response.command_id
    )
    assert command.runtime_session_id == data["session"].id
    assert command.source == "cli"
    assert command.envelope["expires_at"]
    activities = (
        db_session.query(models.RuntimeSessionActivity)
        .filter(models.RuntimeSessionActivity.runtime_session_id == data["session"].id)
        .all()
    )
    assert any(
        (row.metadata_ or {}).get("delivery_route") == "herdr_pane"
        for row in activities
    )


def test_sidecar_resume_precedes_herdr(
    db_session: Session, prompt_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = prompt_data
    data["agent"].runtime_session_id = data["session"].id
    data["agent"].control_last_heartbeat_at = datetime.now(UTC)
    data["session"].control_owned = True
    monkeypatch.setattr(
        "preloop.services.agent_control_dispatch.agent_has_control_config",
        lambda *args, **kwargs: True,
    )
    decision = resolve_session_control_mode(
        db_session, account_id=str(data["runner"].account_id), session=data["session"]
    )
    assert decision.mode == "command" and decision.herdr_target is None


@pytest.mark.asyncio
async def test_consent_denial_precedes_persistence(
    db_session: Session, prompt_data: dict[str, Any], test_user: models.User
) -> None:
    from fastapi import HTTPException
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlSendMessageRequest

    data = prompt_data
    data["agent"].owner_user_id = None
    data["agent"].others_may_start_sessions = "deny"
    with pytest.raises(HTTPException) as error:
        await agent_control._route_managed_agent_prompt(
            db=db_session,
            agent_id=str(data["agent"].id),
            current_user=test_user,
            request=AgentControlSendMessageRequest(
                message="Example",
                target_session_id=data["session"].id,
                spawn_worktree=True,
            ),
        )
    assert error.value.status_code == 403
    assert db_session.query(models.AgentControlCommand).count() == 0


@pytest.mark.asyncio
async def test_public_prompt_requires_control_permission(
    db_session: Session,
    prompt_data: dict[str, Any],
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlSendMessageRequest

    def denied(*args: Any, **kwargs: Any) -> None:
        assert args[-1] == "control_managed_agent"
        raise HTTPException(403, "Permission denied")

    monkeypatch.setattr(agent_control, "ensure_permission_in_oss", denied)
    with pytest.raises(HTTPException) as error:
        await agent_control.send_managed_agent_prompt(
            db=db_session,
            agent_id=str(prompt_data["agent"].id),
            current_user=test_user,
            request=AgentControlSendMessageRequest(
                message="Example", target_session_id=prompt_data["session"].id
            ),
        )
    assert error.value.status_code == 403
    assert db_session.query(models.AgentControlCommand).count() == 0


@pytest.mark.parametrize(
    "result",
    [
        {"code": {}},
        {"delivery": []},
        {"state": {}},
        {"code": "prompt_disabled", "delivery": "sent"},
        {"command_id": "unknown"},
    ],
)
def test_invalid_result_cannot_change_command(
    db_session: Session, prompt_data: dict[str, Any], result: dict[str, Any]
) -> None:
    data = prompt_data
    dispatched = _dispatch(db_session, data)
    herdr_commands.pending_messages(db_session, data["runner"])
    assert not herdr_commands.apply_result(
        db_session,
        data["runner"],
        {
            "command_id": dispatched.command_id,
            "code": "delivered",
            "delivery": "sent",
            **result,
        },
    )
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    assert command.status == "delivered"


@pytest.mark.parametrize(
    "text", ["\n\x1b", "é" * (herdr_commands.MAX_PROMPT_BYTES // 2 + 1)]
)
def test_server_prompt_caps(
    db_session: Session, prompt_data: dict[str, Any], text: str
) -> None:
    from preloop.services.agent_control_dispatch import AgentControlDispatchError

    data = prompt_data
    target = herdr_command.eligible_target(
        db_session,
        account_id=data["runner"].account_id,
        session=data["session"],
        agent=data["agent"],
    )
    with pytest.raises(AgentControlDispatchError):
        herdr_commands.dispatch_prompt(
            db_session,
            agent=data["agent"],
            session=data["session"],
            target=target,
            text=text,
            user_id=data["runner"].registered_by_user_id,
        )
    assert db_session.query(models.AgentControlCommand).count() == 0


def test_agent_plugin_cannot_ack_or_finish_runner_command(
    db_session: Session, prompt_data: dict[str, Any]
) -> None:
    from types import SimpleNamespace
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlInboundEnvelope

    data = prompt_data
    dispatched = _dispatch(db_session, data)
    connection = SimpleNamespace(
        account_id=data["runner"].account_id, managed_agent_id=data["agent"].id
    )
    inbound = AgentControlInboundEnvelope(
        type="status",
        name="command_result",
        payload={"command_id": dispatched.command_id, "status": "completed"},
    )
    agent_control._handle_command_ack(db_session, connection, inbound)
    assert (
        agent_control._persist_agent_control_result(db_session, connection, inbound)
        is None
    )
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    assert command.status == "pending"


@pytest.mark.asyncio
async def test_blocked_pane_returns_actionable_refusal(
    db_session: Session, prompt_data: dict[str, Any], test_user: models.User
) -> None:
    from fastapi import HTTPException
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlSendMessageRequest

    data = prompt_data
    data["session"].terminal_location = data["session"].terminal_location | {
        "state": "blocked"
    }
    with pytest.raises(HTTPException) as error:
        await agent_control._route_managed_agent_prompt(
            db=db_session,
            agent_id=str(data["agent"].id),
            current_user=test_user,
            request=AgentControlSendMessageRequest(
                message="Example", target_session_id=data["session"].id
            ),
        )
    assert (
        error.value.status_code == 409 and error.value.detail["code"] == "agent_blocked"
    )
    assert db_session.query(models.AgentControlCommand).count() == 0


@pytest.mark.parametrize("guard", ["blocked", "disabled", "closed", "working"])
def test_control_schema_and_operator_note_share_refusals(
    db_session: Session, prompt_data: dict[str, Any], test_user: models.User, guard: str
) -> None:
    from fastapi import HTTPException, Request
    from preloop.api.endpoints import agent_control, operator_notes
    from preloop.schemas.operator_notes import OperatorNoteCreate

    data = prompt_data
    location = dict(data["session"].terminal_location)
    if guard == "blocked":
        location["state"] = "blocked"
    if guard == "closed":
        location["pane_closed_at"] = datetime.now(UTC).isoformat()
    if guard == "working":
        data["session"].turn_state = "working"
    if guard == "disabled":
        inventory = data["runner"].harness_inventory
        data["runner"].harness_inventory = inventory | {
            "herdr": inventory["herdr"] | {"prompt_enabled": False}
        }
    data["session"].terminal_location = location
    db_session.flush()
    control = agent_control.get_runtime_session_control_mode(
        runtime_session_id=str(data["session"].id),
        current_user=test_user,
        db=db_session,
    )
    assert control.mode == "note"
    kwargs = {
        "request": Request({"type": "http", "headers": []}),
        "payload": OperatorNoteCreate(
            text="Example", runtime_session_id=data["session"].id
        ),
        "current_user": test_user,
        "db": db_session,
    }
    if guard == "working":
        note = operator_notes.create_operator_note(**kwargs)
        assert note.state == "pending"
    else:
        assert control.reason_code in {"agent_blocked", "herdr_unavailable"}
        with pytest.raises(HTTPException) as error:
            operator_notes.create_operator_note(**kwargs)
        assert error.value.status_code == 409
        assert db_session.query(models.AgentControlCommand).count() == 0


def test_sent_prompt_ending_at_dialog_is_actionable_failure(
    db_session: Session, prompt_data: dict[str, Any]
) -> None:
    from preloop.services.agent_control_dispatch import command_delivery_state

    data = prompt_data
    dispatched = _dispatch(db_session, data)
    herdr_commands.pending_messages(db_session, data["runner"])
    assert herdr_commands.apply_result(
        db_session,
        data["runner"],
        {
            "command_id": dispatched.command_id,
            "code": "agent_blocked",
            "delivery": "sent",
            "state": "blocked",
        },
    )
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    assert command_delivery_state(command)[0] == "failed"
    assert "terminal dialog" in command.last_error
    assert (
        crud_agent_control_command.command_result_payload(command)["delivery"] == "sent"
    )


def test_generic_expiry_does_not_erase_herdr_delivery_audit(
    db_session: Session, prompt_data: dict[str, Any]
) -> None:
    data = prompt_data
    dispatched = _dispatch(db_session, data)
    assert (
        crud_agent_control_command.expire_stale(
            db_session,
            account_id=data["runner"].account_id,
            now=datetime.now(UTC) + timedelta(hours=1),
        )
        == 0
    )
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    assert command.status == "pending"


def test_pending_approval_blocks_pane_prompt(
    db_session: Session, prompt_data: dict[str, Any]
) -> None:
    data = prompt_data
    account_id = data["runner"].account_id
    workflow = models.ApprovalWorkflow(account_id=account_id, name="Example approval")
    tool = models.ToolConfiguration(
        account_id=account_id, tool_name="Example", tool_source="builtin"
    )
    db_session.add_all([workflow, tool])
    db_session.flush()
    db_session.add(
        models.ApprovalRequest(
            account_id=account_id,
            tool_configuration_id=tool.id,
            approval_workflow_id=workflow.id,
            runtime_session_id=data["session"].id,
            tool_name="Example",
            tool_args={},
            status="pending",
        )
    )
    db_session.flush()
    assert (
        herdr_command.eligible_target(
            db_session,
            account_id=account_id,
            session=data["session"],
            agent=data["agent"],
        )
        is None
    )


@pytest.mark.asyncio
async def test_runner_socket_delivers_and_accepts_bound_result(
    db_session: Session, prompt_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.api.endpoints import runners
    from tests.endpoints.test_runner_harness_inventory import _socket

    data = prompt_data
    dispatched = _dispatch(db_session, data)
    sent: list[dict[str, Any]] = []
    socket, _ = _socket(
        monkeypatch,
        data["runner"],
        [
            {
                "type": "herdr_prompt_result",
                "command_id": dispatched.command_id,
                "code": "delivered",
                "delivery": "sent",
                "state": "idle",
            }
        ],
        sent,
    )
    await runners.runner_ws(socket, data["runner"].id, db_session)
    assert any(
        frame.get("type") == "herdr_prompt"
        and frame["command_id"] == dispatched.command_id
        for frame in sent
    )
    command = crud_agent_control_command.get_by_command_id(
        db_session,
        account_id=data["runner"].account_id,
        command_id=dispatched.command_id,
    )
    assert command.status == "acked"


@pytest.mark.asyncio
async def test_absent_herdr_preserves_plugin_requirement(
    db_session: Session, prompt_data: dict[str, Any], test_user: models.User
) -> None:
    from fastapi import HTTPException
    from preloop.api.endpoints import agent_control
    from preloop.schemas.agent_control import AgentControlSendMessageRequest

    data = prompt_data
    data["session"].terminal_location = None
    with patch.object(
        agent_control, "_require_session_start_consent", new_callable=AsyncMock
    ) as consent:
        with pytest.raises(HTTPException) as error:
            await agent_control._route_managed_agent_prompt(
                db=db_session,
                agent_id=str(data["agent"].id),
                current_user=test_user,
                request=AgentControlSendMessageRequest(
                    message="Example", target_session_id=data["session"].id
                ),
            )
    assert error.value.status_code == 409
    assert "plugin configured" in error.value.detail
    consent.assert_not_awaited()
    assert db_session.query(models.AgentControlCommand).count() == 0
