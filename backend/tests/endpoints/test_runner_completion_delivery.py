"""Real database regressions for acknowledged runner completion delivery."""

from unittest.mock import AsyncMock, MagicMock
from typing import Any
from datetime import datetime, timezone

from sqlalchemy import event
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


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only", [True, False])
async def test_completion_revokes_delivered_read_only_leases(
    db_session: Session,
    leased_execution,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    from preloop.services import read_only_review

    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    if read_only:
        assignment = runner.current_assignment
        assignment.pending_job = {
            **assignment.pending_job,
            "host_exec_read_only": {"mode": "read_only", "commit": "a" * 40},
        }
        db_session.commit()
    revoke = AsyncMock(return_value=1)
    monkeypatch.setattr(read_only_review, "revoke_delivered_read_leases", revoke)
    websocket = websocket_for_completion(monkeypatch, runner, execution_id)
    await runners.runner_ws(websocket, runner_id, db_session)
    if read_only:
        revoke.assert_awaited_once_with(execution_id)
    else:
        revoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ["status", "logs", "complete"])
@pytest.mark.parametrize("authorization", ["owner", "other_lease", "revoked"])
async def test_archived_runner_frames_keep_authority_and_skip_workspace(
    db_session: Session,
    leased_execution: tuple[models.FlowRunner, models.FlowExecution],
    monkeypatch: pytest.MonkeyPatch,
    frame: str,
    authorization: str,
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    archive = b"retained workspace" * 70000
    execution.workspace_snapshot = archive
    execution.stop_requested_at = datetime.now(timezone.utc)
    execution.result = {"pr_url": "https://example.com/review/1", "resume_count": 2}
    runner.token_hash = runners.hash_runner_token("synthetic-runner-token")
    db_session.commit()
    authenticate = runners._authenticate_runner
    socket = websocket_for_completion(monkeypatch, runner, execution_id)
    # Exercise the real token verifier and subsequent revocation checks.
    monkeypatch.setattr(runners, "_authenticate_runner", authenticate)
    socket.query_params = {"token": "synthetic-runner-token"}
    socket.headers = {}
    target = uuid4() if authorization == "other_lease" else execution_id
    message: dict[str, Any] = {"type": frame, "execution_id": str(target)}
    if frame == "status":
        message["status"] = "RUNNING"
    elif frame == "logs":
        message.update(lines=["Example log"], batch_id=str(uuid4()))
    else:
        message.update(
            status="SUCCEEDED",
            launch_version=1,
            completion_protocol="docker_v1",
            exit_code=0,
            result={"status": "success"},
        )
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            raise WebSocketDisconnect()
        received = True
        if authorization == "revoked":
            fresh = crud_flow_runner.get(db_session, id=runner_id)
            fresh.token_hash = runners.hash_runner_token("rotated-synthetic-token")
            db_session.commit()
        return message

    socket.receive_json = AsyncMock(side_effect=receive)
    monkeypatch.setattr(
        runners,
        "RunnerModelConnection",
        lambda *args, **kwargs: MagicMock(start=AsyncMock(), close=AsyncMock()),
    )
    monkeypatch.setattr(runners, "_deliver_session_messages", AsyncMock())
    monkeypatch.setattr(runners, "_publish_flow_update", AsyncMock())
    statements: list[str] = []

    def capture(
        _conn: Any, _cursor: Any, sql: str, _params: Any, _context: Any, _many: bool
    ) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    db_session.expunge_all()
    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        await runners.runner_ws(socket, runner_id, db_session)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert all("workspace_snapshot" not in sql for sql in statements), statements
    db_session.expire_all()
    saved = crud_flow_execution.get(db_session, execution_id)
    saved_runner = crud_flow_runner.get(db_session, id=runner_id)
    assert saved.workspace_snapshot == archive
    assert saved.result["pr_url"] == "https://example.com/review/1"
    assert saved.result["resume_count"] == 2
    if frame == "complete" and authorization == "owner":
        assert saved.status == "SUCCEEDED"
        assert saved.stop_confirmed_at is not None
        assert saved.result["status"] == "success"
        assert saved_runner.current_execution_id is None
    else:
        assert saved.status == "RUNNING"
        assert saved.stop_confirmed_at is None
        assert saved_runner.current_execution_id == execution_id
    messages = [call.args[0] for call in socket.send_json.call_args_list]
    if authorization == "revoked":
        assert messages[-1] == {
            "type": "error",
            "error": runners.RUNNER_TOKEN_ROTATED_ERROR,
        }
    elif frame == "logs":
        assert messages[-1] == {
            "type": "logs_ack",
            "execution_id": str(target),
            "batch_id": message["batch_id"],
        }
    else:
        assert messages[-1] == {"type": "ack"}


@pytest.mark.asyncio
async def test_archive_preserved_when_late_completion_confirms_stop(
    db_session: Session,
    leased_execution: tuple[models.FlowRunner, models.FlowExecution],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, execution = leased_execution
    runner_id, execution_id = runner.id, execution.id
    execution.workspace_snapshot = b"retained workspace"
    execution.status = "TIMED_OUT"
    execution.result = {"original": True}
    execution.stop_requested_at = datetime.now(timezone.utc)
    db_session.commit()
    socket = websocket_for_completion(monkeypatch, runner, execution_id)
    await runners.runner_ws(socket, runner_id, db_session)
    db_session.expire_all()
    saved = crud_flow_execution.get(db_session, execution_id)
    assert saved.status == "TIMED_OUT"
    assert saved.result == {"original": True}
    assert saved.stop_confirmed_at is not None
    assert saved.workspace_snapshot == b"retained workspace"
    assert crud_flow_runner.get(db_session, id=runner_id).current_execution_id is None


def test_expired_completion_preserves_evidence_publication_and_continuation(
    db_session: Session,
    leased_execution: tuple[models.FlowRunner, models.FlowExecution],
) -> None:
    from preloop.services.host_exec import apply_runner_completion_to_execution

    runner, execution = leased_execution
    execution_id, account_id, flow_id = (
        execution.id,
        runner.account_id,
        execution.flow_id,
    )
    session_id = str(uuid4())
    receipt = {"pr_url": "https://example.com/review/1"}
    protected = {"phase": "complete", "receipt": receipt}
    execution.workspace_snapshot = b"saved workspace"
    execution.evidence_archive = b"saved evidence"
    execution.result = {
        "_private_publication": protected,
        "trusted_publication": receipt,
        "resume_count": 2,
    }
    parent = models.FlowExecution(
        flow_id=flow_id, status="RESUMING", resume_execution_id=execution_id
    )
    db_session.add(parent)
    db_session.commit()
    parent_id = parent.id
    locked = crud_flow_execution.lock_for_runner_completion(
        db_session, execution_id=execution_id, account_id=account_id
    )
    db_session.expire_all()  # The same invalidation performed by lease clearing.
    statements: list[str] = []

    def capture(
        _conn: Any, _cursor: Any, sql: str, _params: Any, _context: Any, _many: bool
    ) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(sql)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        crud_flow_execution.refresh_runner_metadata(db_session, locked)
        apply_runner_completion_to_execution(
            db_session,
            locked,
            account_id=account_id,
            status="SUCCEEDED",
            error=None,
            result={
                "status": "success",
                "harness": "copilot_cli",
                "session_id": session_id,
                "premium_requests": 3,
                "gateway_metered": False,
            },
            message={"status": "SUCCEEDED", "evidence_upload": "absent"},
            pending_job={
                "completion_protocol": "host_exec",
                "agent_type": "copilot",
                "host_exec_profile": "copilot-seat",
                "host_exec_publication": {"mode": "legacy"},
            },
        )
        db_session.commit()
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert all("workspace_snapshot" not in sql for sql in statements), statements
    saved = crud_flow_execution.get(db_session, execution_id)
    assert saved.workspace_snapshot == b"saved workspace"
    assert saved.evidence_archive == b"saved evidence"
    assert saved.evidence_receipt["status"] == "missing"
    assert saved.result["_private_publication"] == protected
    assert saved.result["trusted_publication"] == receipt
    assert saved.result["resume_count"] == 2
    assert saved.cli_session["session_id"] == session_id
    assert crud_flow_execution.get(db_session, parent_id).status == "SUCCEEDED"
    usage = (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.flow_execution_id == execution_id)
        .all()
    )
    premium = [
        row
        for row in usage
        if (row.meta_data or {}).get("event_type") == "host_exec_result"
    ]
    assert len(premium) == 1
