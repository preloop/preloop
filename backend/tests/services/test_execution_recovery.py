"""Unit tests for the orphaned flow-execution recovery service."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.agents.base import AgentStatus
from preloop.services import execution_recovery
from preloop.services.execution_recovery import (
    ExecutionRecoveryService,
    _exception_message,
    get_recovery_service,
)


def _make_execution(*, agent_session_reference="sess-ref", status="RUNNING"):
    execution = MagicMock()
    execution.id = uuid.uuid4()
    execution.flow_id = uuid.uuid4()
    execution.status = status
    execution.agent_session_reference = agent_session_reference
    execution.trigger_event_details = {}
    flow = MagicMock()
    flow.agent_type = "openhands"
    flow.agent_config = {}
    execution.flow = flow
    return execution


# --- _exception_message ----------------------------------------------------


def test_exception_message_uses_str_when_present():
    assert _exception_message(ValueError("boom")) == "boom"


def test_exception_message_falls_back_to_class_name():
    assert _exception_message(ValueError()) == "ValueError"


# --- get_recovery_service singleton ----------------------------------------


def test_get_recovery_service_is_singleton():
    execution_recovery._recovery_service = None
    first = get_recovery_service()
    second = get_recovery_service()
    assert first is second
    assert isinstance(first, ExecutionRecoveryService)


# --- recover_orphaned_executions -------------------------------------------


@pytest.mark.asyncio
async def test_recover_no_orphans_returns_zero():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    with patch.object(
        execution_recovery.crud_flow_execution, "get_multi", return_value=[]
    ):
        recovered = await svc.recover_orphaned_executions(db)
    assert recovered == 0


@pytest.mark.asyncio
async def test_recover_counts_successful_resumes():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    execs = [_make_execution(), _make_execution()]

    def _get_multi(db, *, skip, limit, status):
        # Only the RUNNING bucket has orphans; the rest are empty.
        return execs if status == "RUNNING" else []

    svc._resume_execution_monitoring = AsyncMock()
    with (
        patch.object(
            execution_recovery.crud_flow_execution, "get_multi", side_effect=_get_multi
        ),
        patch.object(
            execution_recovery, "get_nats_client", AsyncMock(return_value="nats")
        ),
    ):
        recovered = await svc.recover_orphaned_executions(db)
    assert recovered == 2
    assert svc._resume_execution_monitoring.await_count == 2


@pytest.mark.asyncio
async def test_recover_handles_nats_connection_failure():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    execs = [_make_execution()]

    captured = {}

    async def _resume(db_arg, execution, nats_client):
        captured["nats"] = nats_client

    svc._resume_execution_monitoring = AsyncMock(side_effect=_resume)
    with (
        patch.object(
            execution_recovery.crud_flow_execution,
            "get_multi",
            side_effect=lambda db, *, skip, limit, status: execs
            if status == "RUNNING"
            else [],
        ),
        patch.object(
            execution_recovery,
            "get_nats_client",
            AsyncMock(side_effect=RuntimeError("no nats")),
        ),
    ):
        recovered = await svc.recover_orphaned_executions(db)
    assert recovered == 1
    # NATS unavailability degrades gracefully to a None client.
    assert captured["nats"] is None


@pytest.mark.asyncio
async def test_recover_continues_when_one_resume_fails():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    bad = _make_execution()
    good = _make_execution()

    async def _resume(db_arg, execution, nats_client):
        if execution is bad:
            raise RuntimeError("resume failed")

    svc._resume_execution_monitoring = AsyncMock(side_effect=_resume)
    with (
        patch.object(
            execution_recovery.crud_flow_execution,
            "get_multi",
            side_effect=lambda db, *, skip, limit, status: [bad, good]
            if status == "RUNNING"
            else [],
        ),
        patch.object(
            execution_recovery, "get_nats_client", AsyncMock(return_value="nats")
        ),
    ):
        recovered = await svc.recover_orphaned_executions(db)
    # Only the good execution counts; the failure is swallowed.
    assert recovered == 1


# --- _resume_execution_monitoring ------------------------------------------


@pytest.mark.asyncio
async def test_resume_marks_failed_when_no_agent_session():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    execution = _make_execution(agent_session_reference=None)
    with patch.object(execution_recovery.crud_flow_execution, "update") as update_mock:
        await svc._resume_execution_monitoring(db, execution, None)
    update_mock.assert_called_once()
    update_data = update_mock.call_args.kwargs["obj_in"]
    assert update_data.status == "FAILED"
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_resume_marks_failed_when_container_failed():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    execution = _make_execution()

    agent_executor = MagicMock()
    agent_executor.get_status = AsyncMock(return_value=AgentStatus.FAILED)
    agent_executor.aclose = AsyncMock()

    with (
        patch("preloop.agents.create_agent_executor", return_value=agent_executor),
        patch.object(execution_recovery.crud_flow_execution, "update") as update_mock,
    ):
        await svc._resume_execution_monitoring(db, execution, None)
    update_mock.assert_called_once()
    assert update_mock.call_args.kwargs["obj_in"].status == "FAILED"
    agent_executor.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_marks_succeeded_when_container_succeeded():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    execution = _make_execution()

    agent_executor = MagicMock()
    agent_executor.get_status = AsyncMock(return_value=AgentStatus.SUCCEEDED)
    agent_executor.aclose = AsyncMock()

    with (
        patch("preloop.agents.create_agent_executor", return_value=agent_executor),
        patch.object(execution_recovery.crud_flow_execution, "update") as update_mock,
    ):
        await svc._resume_execution_monitoring(db, execution, None)
    assert update_mock.call_args.kwargs["obj_in"].status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_resume_marks_failed_when_status_check_raises():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    execution = _make_execution()

    with (
        patch(
            "preloop.agents.create_agent_executor",
            side_effect=RuntimeError("docker down"),
        ),
        patch.object(execution_recovery.crud_flow_execution, "update") as update_mock,
    ):
        await svc._resume_execution_monitoring(db, execution, None)
    update_mock.assert_called_once()
    update_data = update_mock.call_args.kwargs["obj_in"]
    assert update_data.status == "FAILED"
    assert "docker down" in update_data.error_message


@pytest.mark.asyncio
async def test_resume_running_container_schedules_monitoring_task():
    svc = ExecutionRecoveryService()
    db = MagicMock()
    execution = _make_execution()

    agent_executor = MagicMock()
    agent_executor.get_status = AsyncMock(return_value=AgentStatus.RUNNING)
    agent_executor.aclose = AsyncMock()

    fake_task = MagicMock()
    with (
        patch("preloop.agents.create_agent_executor", return_value=agent_executor),
        patch.object(execution_recovery, "FlowExecutionOrchestrator") as orch_cls,
        patch.object(
            execution_recovery.asyncio, "create_task", return_value=fake_task
        ) as create_task_mock,
    ):
        await svc._resume_execution_monitoring(db, execution, "nats")
    orch_cls.assert_called_once()
    create_task_mock.assert_called_once()
    assert svc.recovery_tasks == [fake_task]


# --- wait_for_completion ---------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_completion_no_tasks():
    svc = ExecutionRecoveryService()
    # Should return immediately without error.
    await svc.wait_for_completion(timeout=1)


@pytest.mark.asyncio
async def test_wait_for_completion_awaits_tasks():
    svc = ExecutionRecoveryService()

    async def _quick():
        return None

    svc.recovery_tasks = [asyncio.create_task(_quick())]
    await svc.wait_for_completion(timeout=5)
    assert all(t.done() for t in svc.recovery_tasks)


@pytest.mark.asyncio
async def test_wait_for_completion_timeout_cancels_pending_tasks():
    svc = ExecutionRecoveryService()
    pending = MagicMock()
    pending.done.return_value = False
    svc.recovery_tasks = [pending]
    with (
        patch.object(execution_recovery.asyncio, "gather", return_value=MagicMock()),
        patch.object(
            execution_recovery.asyncio,
            "wait_for",
            AsyncMock(side_effect=asyncio.TimeoutError),
        ),
    ):
        await svc.wait_for_completion(timeout=1)
    pending.cancel.assert_called_once()
