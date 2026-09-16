"""Real database regressions for acknowledged runner completion delivery."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import (
    FlowExecutionCreate,
    FlowExecutionUpdate,
)


@pytest.fixture
def leased_execution(
    db_session: Session,
) -> tuple[models.FlowRunner, models.FlowExecution]:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Runner delivery test"}
    )
    flow = crud_flow.create(
        db_session,
        account_id=account.id,
        flow_in=FlowCreate(
            name="Completion delivery",
            account_id=account.id,
            agent_type="codex",
            agent_config={},
            prompt_template="Implement the issue",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
        ),
    )
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "name": "local runner",
            "token_hash": "test-token",
            "status": "busy",
            "concurrency": 1,
        },
    )
    assignment = crud_flow_runner.create_assignment(
        db_session,
        runner_id=runner.id,
        execution_id=execution.id,
        pending_job={
            "execution_id": str(execution.id),
            "launch_version": 1,
            "agent_type": "codex",
        },
    )
    assignment.reported_status = "RUNNING"
    db_session.commit()
    return runner, execution


def websocket_for_completion(
    monkeypatch: pytest.MonkeyPatch,
    runner: models.FlowRunner,
    execution_id: object,
) -> MagicMock:
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[
            {
                "type": "complete",
                "execution_id": str(execution_id),
                "status": "SUCCEEDED",
                "launch_version": 1,
                "completion_protocol": "docker_v1",
                "exit_code": 0,
                "result": {"status": "success"},
            },
            WebSocketDisconnect(),
        ]
    )
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(
        runners, "job_for_runner_replay", lambda db, pending_job, **kwargs: pending_job
    )
    monkeypatch.setattr(runners, "prepare_runner_delivery", AsyncMock(return_value={}))
    return websocket


@pytest.mark.asyncio
async def test_completion_persistence_failure_retains_runner_lease(
    db_session: Session, leased_execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    websocket = websocket_for_completion(monkeypatch, runner, execution_id)
    persist = runners.apply_runner_completion_to_execution

    def persist_then_fail(*args, **kwargs) -> None:
        persist(*args, **kwargs)
        raise RuntimeError("simulated persistence failure before commit")

    monkeypatch.setattr(
        runners, "apply_runner_completion_to_execution", persist_then_fail
    )
    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        await runners.runner_ws(websocket, runner_id, db_session)
    db_session.expire_all()
    saved_runner = crud_flow_runner.get(db_session, id=runner_id)
    saved_execution = crud_flow_execution.get(db_session, id=execution_id)
    assert saved_runner.current_execution_id == execution_id
    assert saved_runner.pending_job["execution_id"] == str(execution_id)
    assert saved_runner.current_assignment.reported_status == "RUNNING"
    assert saved_execution.status == "RUNNING"
    assert saved_execution.result is None


@pytest.mark.asyncio
async def test_completion_commits_result_and_releases_lease_together(
    db_session: Session, leased_execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    websocket = websocket_for_completion(monkeypatch, runner, execution_id)
    await runners.runner_ws(websocket, runner_id, db_session)
    db_session.expire_all()
    saved_runner = crud_flow_runner.get(db_session, id=runner_id)
    saved_execution = crud_flow_execution.get(db_session, id=execution_id)
    assert saved_runner.current_execution_id is None
    assert saved_runner.pending_job is None
    assert saved_runner.running_count == 0
    assert saved_runner.free_slots == saved_runner.capacity
    assert saved_execution.status == "SUCCEEDED"
    assert saved_execution.result["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    ["FAILED", "STOPPED", "CANCELLED", "TIMEOUT", "TIMED_OUT", "ABORTED"],
)
async def test_late_owner_completion_preserves_existing_terminal_outcome(
    db_session: Session,
    leased_execution,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    crud_flow_execution.update(
        db_session,
        db_obj=execution,
        obj_in=FlowExecutionUpdate(
            status=terminal_status,
            error_message="original terminal reason",
            result={"original": True},
        ),
    )
    db_session.commit()
    websocket = websocket_for_completion(monkeypatch, runner, execution_id)
    await runners.runner_ws(websocket, runner_id, db_session)
    db_session.expire_all()
    saved_runner = crud_flow_runner.get(db_session, id=runner_id)
    saved_execution = crud_flow_execution.get(db_session, id=execution_id)
    assert saved_runner.current_execution_id is None
    assert saved_runner.running_count == 0
    assert saved_execution.status == terminal_status
    assert saved_execution.error_message == "original terminal reason"
    assert saved_execution.result == {"original": True}


@pytest.mark.asyncio
async def test_replayed_completion_cannot_finish_a_different_lease(
    db_session: Session, leased_execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    websocket = websocket_for_completion(monkeypatch, runner, uuid4())
    await runners.runner_ws(websocket, runner_id, db_session)
    db_session.expire_all()
    saved_runner = crud_flow_runner.get(db_session, id=runner_id)
    saved_execution = crud_flow_execution.get(db_session, id=execution_id)
    assert saved_runner.current_execution_id == execution_id
    assert saved_execution.status == "RUNNING"
    assert saved_execution.result is None


@pytest.mark.asyncio
async def test_log_batch_replay_is_idempotent_in_postgres(
    db_session: Session, leased_execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.models.crud import crud_flow_execution_log

    _, execution = leased_execution
    monkeypatch.setattr(runners, "_publish_flow_update", AsyncMock())
    batch_id = str(uuid4())
    for _ in range(2):
        await runners.persist_runner_logs(
            db_session, execution.id, ["native session", "PR created"], batch_id
        )
    stored = crud_flow_execution_log.get_agent_log_page(db_session, execution.id)
    assert len(stored) == 2
    assert {line.message for line in stored} == {"native session", "PR created"}


@pytest.mark.asyncio
async def test_log_ack_releases_read_transaction_before_next_frame(
    db_session: Session, leased_execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    websocket = websocket_for_completion(monkeypatch, runner, execution_id)
    websocket.receive_json = AsyncMock(
        side_effect=[
            {
                "type": "logs",
                "execution_id": str(execution_id),
                "batch_id": str(uuid4()),
                "lines": ["ordinary progress line"],
            },
            WebSocketDisconnect(),
        ]
    )
    monkeypatch.setattr(runners, "_publish_flow_update", AsyncMock())
    acknowledgements = []

    async def check_ack(message: dict) -> None:
        if message.get("type") == "logs_ack":
            assert not db_session.in_transaction()
            acknowledgements.append(message)

    websocket.send_json = AsyncMock(side_effect=check_ack)
    await runners.runner_ws(websocket, runner_id, db_session)
    assert len(acknowledgements) == 1


@pytest.mark.asyncio
async def test_missing_execution_lookup_does_not_clear_owner_lease(
    db_session: Session, leased_execution, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    websocket = websocket_for_completion(monkeypatch, runner, execution_id)
    monkeypatch.setattr(
        runners.crud_flow_execution, "lock_for_runner_completion", lambda *a, **kw: None
    )
    await runners.runner_ws(websocket, runner_id, db_session)
    db_session.expire_all()
    assert (
        crud_flow_runner.get(db_session, id=runner_id).current_execution_id
        == execution_id
    )
    errors = [
        call.args[0]
        for call in websocket.send_json.call_args_list
        if call.args[0].get("type") == "error"
    ]
    assert errors == [{"type": "error", "error": "Runner execution no longer exists"}]
