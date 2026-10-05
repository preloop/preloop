"""A stop that lands while a run is being launched is durable.

The stop command is sent on core NATS, which nobody listens to until the
orchestrator starts monitoring, and the stop found no runtime reference to
tear down while the runtime was still being created. These tests pause the
orchestrator at each launch boundary, stop the execution through the same
code path the API uses, and then let the orchestrator continue. Whatever the
boundary, the stopped row never becomes RUNNING again, a runtime created in
the race is torn down, and ``stop_confirmed_at`` is only written once the
runtime is verified gone.
"""

import asyncio
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.agents.base import AgentStatus
from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    crud_user,
)
from preloop.models.models import Account, Flow
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services import flow_orchestrator as orchestrator_module
from preloop.services.flow_execution_stop import (
    NO_RUNTIME_REFERENCE_REASON,
    STOP_REQUESTED_AUDIT_ACTION,
    stop_execution,
)
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.fixture
def account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Stop Race {uuid4().hex[:8]}", "is_active": True},
    )


@pytest.fixture
def user(db_session: Session, account: Account) -> User:
    created = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": f"stop_race_{uuid4().hex[:8]}@example.com",
            "username": f"stop_race_{uuid4().hex[:8]}",
            "full_name": "Stop Race",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db_session.flush()
    account.primary_user_id = created.id
    db_session.add(account)
    db_session.commit()
    return created


@pytest.fixture
def flow(db_session: Session, account: Account, user: User) -> Flow:
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Stop race flow",
            trigger_event_source="github",
            trigger_event_types=["pull_request_updated"],
            prompt_template="Review: {{payload.pull_request.title}}",
            agent_type="codex",
            agent_config={},
            account_id=account.id,
            timeout_seconds=60,
        ),
        account_id=account.id,
    )


@pytest.fixture
def nats_client():
    client = AsyncMock()
    client.is_connected = True
    client.publish = AsyncMock()
    return client


@pytest.fixture(autouse=True)
def quiet_side_channels():
    """NATS stop commands go nowhere (nobody is subscribed during launch)."""
    with (
        patch.object(FlowExecutionOrchestrator, "send_command", new_callable=AsyncMock),
        patch.object(
            FlowExecutionOrchestrator, "_update_commit_status", new_callable=AsyncMock
        ),
    ):
        yield


def _event_data():
    return {
        "source": "github",
        "type": "pull_request_updated",
        "event_id": f"evt_{uuid4().hex[:8]}",
        "payload": {
            "pull_request": {"title": "Add a feature", "number": 7},
            "repository": "example-org/example-repo",
        },
        "account_id": str(uuid4()),
    }


class StatusRecorder:
    """Every launch status the orchestrator tried to write, and the verdict."""

    def __init__(self):
        self.claims: List[tuple] = []
        self._original = crud_flow_execution.claim_live_status

    def __call__(self, db, *, execution_id, status):
        won = self._original(db, execution_id=execution_id, status=status)
        self.claims.append((status, won))
        return won


def _executor(session_reference: str = "agent-race-1") -> MagicMock:
    executor = MagicMock()
    executor.start = AsyncMock(return_value=session_reference)
    executor.stop = AsyncMock()
    executor.is_stopped = AsyncMock(return_value=True)
    executor.cleanup = AsyncMock()
    return executor


async def _stop(db_session, execution, user):
    return await stop_execution(
        db_session,
        execution,
        account_id=user.account_id,
        nats_client=None,
        user_id=user.id,
    )


def _never_running_after_stop(recorder: StatusRecorder) -> None:
    lost = [status for status, won in recorder.claims if not won]
    assert lost, "the launch should have lost a status write to the stop"
    assert ("RUNNING", True) not in recorder.claims


@pytest.mark.asyncio
async def test_stop_before_admission_never_launches(
    db_session, flow, user, nats_client
):
    """Stopped between STARTING and admission: no runtime is created.

    The executor is built right before admission; that hook is synchronous,
    so the stop is applied there through the stop path's own status write
    (what ``stop_execution`` writes for a row with no runtime reference).
    """
    executor = _executor()
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=_event_data(),
        nats_client=nats_client,
    )
    recorder = StatusRecorder()

    def create_executor_then_stop(*_args, **_kwargs):
        assert crud_flow_execution.mark_stopped(
            db_session,
            execution_id=orchestrator.execution_log.id,
            error_message="Manually stopped by user",
            unconfirmed_reason=NO_RUNTIME_REFERENCE_REASON,
            confirm_if_never_launched=True,
        )
        return executor

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            side_effect=create_executor_then_stop,
        ),
        patch.object(crud_flow_execution, "claim_live_status", side_effect=recorder),
    ):
        await orchestrator.run()

    row = crud_flow_execution.get(db_session, id=orchestrator.execution_log.id)
    db_session.refresh(row)
    executor.start.assert_not_awaited()
    assert ("STARTING", True) in recorder.claims
    assert ("RUNNING", True) not in recorder.claims
    assert row.status == "STOPPED"
    assert row.error_message == "Manually stopped by user"
    assert row.stop_requested_at is not None
    assert row.stop_source == "manual"
    assert row.launch_requested_at is None
    # Nothing was ever launched, so nothing is left to terminate.
    assert row.stop_confirmed_at is not None
    assert row.agent_session_reference is None


@pytest.mark.asyncio
async def test_stop_during_initializing_refuses_starting(
    db_session, flow, user, nats_client
):
    """Stopped while the context is being prepared: STARTING is refused."""
    executor = _executor()
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=_event_data(),
        nats_client=nats_client,
    )
    recorder = StatusRecorder()
    original_prepare = FlowExecutionOrchestrator._prepare_execution_context

    async def prepare_then_stop(self):
        context = await original_prepare(self)
        outcome = await _stop(db_session, self.execution_log, user)
        assert outcome.stopped is True
        return context

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(
            FlowExecutionOrchestrator,
            "_prepare_execution_context",
            prepare_then_stop,
        ),
        patch.object(crud_flow_execution, "claim_live_status", side_effect=recorder),
    ):
        await orchestrator.run()

    row = crud_flow_execution.get(db_session, id=orchestrator.execution_log.id)
    db_session.refresh(row)
    _never_running_after_stop(recorder)
    assert ("STARTING", False) in recorder.claims
    executor.start.assert_not_awaited()
    assert row.status == "STOPPED"
    assert row.error_message == "Manually stopped by user"
    assert row.stop_confirmed_at is not None


@pytest.mark.asyncio
async def test_stop_while_runtime_starts_tears_the_runtime_down(
    db_session, flow, user, nats_client
):
    """Stopped inside ``executor.start()``: the runtime it creates is stopped.

    The stop finds no runtime reference (the RUNNING write carries it), so
    the orchestrator owns the teardown when its RUNNING write loses.
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    executor = _executor("agent-race-starting")

    async def slow_start(_context):
        entered.set()
        await release.wait()
        return "agent-race-starting"

    executor.start = AsyncMock(side_effect=slow_start)
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=_event_data(),
        nats_client=nats_client,
    )
    recorder = StatusRecorder()
    monitor = AsyncMock()

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(crud_flow_execution, "claim_live_status", side_effect=recorder),
        patch.object(FlowExecutionOrchestrator, "_monitor_agent_execution", monitor),
    ):
        run = asyncio.create_task(orchestrator.run())
        await asyncio.wait_for(entered.wait(), timeout=10)
        row = crud_flow_execution.get(db_session, id=orchestrator.execution_log.id)
        db_session.refresh(row)
        assert row.status == "STARTING"

        outcome = await _stop(db_session, row, user)
        assert outcome.stopped is True
        db_session.refresh(row)
        # Requested, not confirmed: the runtime is still being created.
        assert row.status == "STOPPED"
        assert row.stop_requested_at is not None
        assert row.stop_confirmed_at is None
        assert NO_RUNTIME_REFERENCE_REASON in (row.stop_reason or "")

        release.set()
        await asyncio.wait_for(run, timeout=10)

    db_session.refresh(row)
    _never_running_after_stop(recorder)
    monitor.assert_not_awaited()
    executor.stop.assert_awaited_once_with("agent-race-starting")
    executor.cleanup.assert_awaited()
    assert row.status == "STOPPED"
    assert row.error_message == "Manually stopped by user"
    assert row.agent_session_reference == "agent-race-starting"
    assert row.stop_confirmed_at is not None
    # Confirmed termination must not keep the "not confirmed" sentence.
    assert NO_RUNTIME_REFERENCE_REASON not in (row.stop_reason or "")

    audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.resource_id == str(row.id),
            AuditLog.action == STOP_REQUESTED_AUDIT_ACTION,
        )
        .all()
    )
    assert len(audit) == 1
    assert audit[0].user_id == user.id
    assert audit[0].details["stop_source"] == "manual"
    assert audit[0].details["termination_confirmed"] is False


@pytest.mark.asyncio
async def test_runtime_created_in_the_race_stays_unconfirmed_until_gone(
    db_session, flow, user, nats_client, monkeypatch
):
    """Delayed deletion: no confirmation until the runtime reports it is gone."""
    monkeypatch.setattr(orchestrator_module, "LAUNCH_RACE_STOP_CONFIRM_ATTEMPTS", 3)
    monkeypatch.setattr(
        orchestrator_module, "LAUNCH_RACE_STOP_CONFIRM_INTERVAL_SECONDS", 0
    )
    executor = _executor("agent-race-slow-delete")
    executor.is_stopped = AsyncMock(return_value=False)
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=_event_data(),
        nats_client=nats_client,
    )

    async def start_and_get_stopped(_context):
        await _stop(db_session, orchestrator.execution_log, user)
        return "agent-race-slow-delete"

    executor.start = AsyncMock(side_effect=start_and_get_stopped)

    with patch(
        "preloop.services.flow_orchestrator.create_executor_for_execution",
        return_value=executor,
    ):
        await orchestrator.run()

    row = crud_flow_execution.get(db_session, id=orchestrator.execution_log.id)
    db_session.refresh(row)
    assert row.status == "STOPPED"
    assert row.stop_requested_at is not None
    assert row.stop_confirmed_at is None
    assert row.agent_session_reference == "agent-race-slow-delete"
    assert executor.is_stopped.await_count == 3

    # The recovery pass resumes monitoring of an unconfirmed stop; the
    # monitor confirms once the runtime is actually gone.
    candidates = crud_flow_execution.list_stale_or_unclaimed_active(db_session)
    assert row.id in {candidate.id for candidate in candidates}


@pytest.mark.asyncio
async def test_monitor_confirms_a_manual_stop_only_after_termination(
    db_session, flow, user, nats_client, monkeypatch
):
    """Kubernetes deletion accepted, pods still terminating, then gone."""
    for name in (
        "_listen_for_commands",
        "_stream_logs_to_nats",
        "_cleanup_monitoring",
    ):
        monkeypatch.setattr(FlowExecutionOrchestrator, name, AsyncMock())
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_capture_result_artifact",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_sync_runtime_tool_activity_metrics",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", AsyncMock())

    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=_event_data(),
        nats_client=nats_client,
    )
    orchestrator._get_flow_details()
    orchestrator._create_execution_log()
    row = orchestrator.execution_log
    row.status = "RUNNING"
    row.agent_session_reference = "agent-k8s-job"
    db_session.commit()

    # The API's stop: the Job deletion is accepted, the pods are not gone.
    api_runtime = MagicMock()
    api_runtime.get_logs = AsyncMock(return_value=[])
    api_runtime.stop = AsyncMock()
    api_runtime.is_stopped = AsyncMock(return_value=False)
    with patch("preloop.agents.codex.CodexAgent", return_value=api_runtime):
        outcome = await _stop(db_session, row, user)
    assert outcome.stopped is True
    db_session.refresh(row)
    assert row.status == "STOPPED"
    assert row.stop_requested_at is not None
    assert row.stop_confirmed_at is None
    api_runtime.stop.assert_awaited_once_with("agent-k8s-job")

    executor = MagicMock()
    executor.stop = AsyncMock()
    executor.is_stopped = AsyncMock(side_effect=[False, False, True])
    executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
    result = await orchestrator._monitor_agent_execution("agent-k8s-job", executor)

    db_session.refresh(row)
    assert result["status"] == "STOPPED"
    assert "stopped by user request" in result["error_message"]
    assert executor.is_stopped.await_count == 3
    assert row.stop_confirmed_at is not None


@pytest.mark.asyncio
async def test_result_reported_after_a_stop_does_not_replace_it(
    db_session, flow, user, nats_client
):
    """The agent finishes after the operator stopped it: STOPPED stands."""
    executor = _executor("agent-finishes-late")
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data=_event_data(),
        nats_client=nats_client,
    )

    async def monitor(_session_reference, _executor):
        api_runtime = MagicMock()
        api_runtime.get_logs = AsyncMock(return_value=[])
        api_runtime.stop = AsyncMock()
        api_runtime.is_stopped = AsyncMock(return_value=True)
        with patch("preloop.agents.codex.CodexAgent", return_value=api_runtime):
            await _stop(db_session, orchestrator.execution_log, user)
        return {
            "status": "SUCCEEDED",
            "output_summary": "done",
            "error_message": None,
            "exit_code": 0,
            "actions_taken": [],
            "mcp_usage_logs": [],
        }

    file_follow_ups = AsyncMock()
    notify = AsyncMock()
    commit_status = AsyncMock()
    queued_followup = AsyncMock()
    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(
            FlowExecutionOrchestrator, "_monitor_agent_execution", side_effect=monitor
        ),
        patch.object(
            FlowExecutionOrchestrator,
            "_file_approved_follow_ups",
            file_follow_ups,
        ),
        patch.object(FlowExecutionOrchestrator, "_notify_terminal", notify),
        patch.object(FlowExecutionOrchestrator, "_update_commit_status", commit_status),
        patch.object(
            FlowExecutionOrchestrator, "_start_queued_followup", queued_followup
        ),
    ):
        await orchestrator.run()

    row = crud_flow_execution.get(db_session, id=orchestrator.execution_log.id)
    db_session.refresh(row)
    assert row.status == "STOPPED"
    assert row.error_message == "Manually stopped by user"
    assert row.stop_confirmed_at is not None
    file_follow_ups.assert_not_awaited()
    queued_followup.assert_not_awaited()
    assert notify.await_args.kwargs["status"] == "STOPPED"
    assert "success" not in {
        call.kwargs.get("state") for call in commit_status.await_args_list
    }
    assert "failure" in {
        call.kwargs.get("state") for call in commit_status.await_args_list
    }
