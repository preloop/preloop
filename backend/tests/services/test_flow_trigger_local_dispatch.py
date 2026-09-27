"""Supervision of the in-process (non-worker) flow dispatch task.

When no execution worker is enabled, ``_start_flow_execution`` runs the flow
on an ``asyncio`` task in the same process. These tests pin down that the task
is referenced until it finishes and that an exception it raises is logged and
recorded on the execution instead of stranding the row in PENDING.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Set
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import flow_trigger_service as fts
from preloop.services.flow_failure_category import FAILURE_CATEGORY_RUNNER_ERROR
from preloop.services.flow_trigger_service import FlowTriggerService

pytestmark = pytest.mark.asyncio


def _flow(db_session: Session, account_id: Any) -> Any:
    """Persist a minimal enabled flow for the account."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"local-dispatch-{uuid.uuid4()}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )
    db_session.commit()
    db_session.refresh(flow)
    return flow


def _pending_execution(db_session: Session, flow: Any) -> Any:
    """Persist a PENDING execution for ``flow``."""
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="PENDING",
            trigger_event_details={"source": "github", "type": "test"},
        ),
    )
    db_session.commit()
    db_session.refresh(execution)
    return execution


def _same_connection_factory(db_session: Session) -> Callable[[], Session]:
    """Session factory sharing the test transaction, so the rollback cleans up."""
    return lambda: Session(bind=db_session.connection())


async def _await_new_local_tasks(before: Set["asyncio.Task[None]"]) -> None:
    """Wait for the local-dispatch tasks started since ``before`` to finish."""
    new_tasks = [
        t for t in fts._LOCAL_RUN_TASKS - before if isinstance(t, asyncio.Task)
    ]
    assert len(new_tasks) == 1, "exactly one local dispatch task is expected"
    await asyncio.gather(*new_tasks, return_exceptions=True)
    # Done-callbacks are scheduled with call_soon; let them run.
    await asyncio.sleep(0)
    await _await_failure_writes()


async def _await_failure_writes() -> None:
    """Wait for the worker-thread failure writes and their done-callbacks."""
    await asyncio.gather(*fts._LOCAL_RUN_FAILURE_WRITES, return_exceptions=True)
    await asyncio.sleep(0)


class TestLocalDispatchFailure:
    """A raising local run leaves the execution FAILED with the error."""

    async def test_raising_local_run_marks_execution_failed(
        self, db_session: Session, test_user: Any
    ) -> None:
        flow = _flow(db_session, test_user.account_id)
        execution = _pending_execution(db_session, flow)
        service = FlowTriggerService(
            db_session, session_factory=_same_connection_factory(db_session)
        )

        async def _boom(_orchestrator: Any) -> None:
            raise RuntimeError("orchestrator exploded before launch")

        before = set(fts._LOCAL_RUN_TASKS)
        with (
            patch(
                "preloop.services.flow_execution_dispatcher."
                "flow_execution_worker_enabled",
                return_value=False,
            ),
            patch(
                "preloop.services.flow_execution_runner.run_existing_execution",
                side_effect=_boom,
            ),
            patch.object(fts, "FlowExecutionOrchestrator", MagicMock()),
            patch.object(fts, "logger") as logger,
        ):
            returned = await service._start_flow_execution(
                flow,
                {"source": "github", "type": "test"},
                MagicMock(),
                precreated_execution=execution,
            )
            await _await_new_local_tasks(before)

        assert returned is execution
        db_session.expire_all()
        stored = crud_flow_execution.get(db_session, id=execution.id)
        assert stored.status == "FAILED"
        assert "RuntimeError" in stored.error_message
        assert "orchestrator exploded before launch" in stored.error_message
        assert stored.failure_category == FAILURE_CATEGORY_RUNNER_ERROR
        assert stored.end_time is not None
        logged = logger.error.call_args
        assert logged.args[1] == execution.id
        assert isinstance(logged.kwargs["exc_info"], RuntimeError)
        assert not (fts._LOCAL_RUN_TASKS - before)
        assert not fts._LOCAL_RUN_FAILURE_WRITES

    async def test_successful_local_run_leaves_execution_alone(
        self, db_session: Session, test_user: Any
    ) -> None:
        flow = _flow(db_session, test_user.account_id)
        execution = _pending_execution(db_session, flow)
        service = FlowTriggerService(
            db_session, session_factory=_same_connection_factory(db_session)
        )

        async def _ok(_orchestrator: Any) -> None:
            return None

        before = set(fts._LOCAL_RUN_TASKS)
        with (
            patch(
                "preloop.services.flow_execution_dispatcher."
                "flow_execution_worker_enabled",
                return_value=False,
            ),
            patch(
                "preloop.services.flow_execution_runner.run_existing_execution",
                side_effect=_ok,
            ),
            patch.object(fts, "FlowExecutionOrchestrator", MagicMock()),
        ):
            await service._start_flow_execution(
                flow,
                {"source": "github", "type": "test"},
                MagicMock(),
                precreated_execution=execution,
            )
            await _await_new_local_tasks(before)

        db_session.expire_all()
        stored = crud_flow_execution.get(db_session, id=execution.id)
        assert stored.status == "PENDING"
        assert stored.error_message is None
        assert not (fts._LOCAL_RUN_TASKS - before)


def _finished_task(exc: BaseException | None = None) -> "asyncio.Task[None]":
    """Return a real, already finished task raising ``exc`` (or returning)."""

    async def _run() -> None:
        if exc is not None:
            raise exc

    return asyncio.get_running_loop().create_task(_run())


class TestSuperviseLocalRun:
    """The done-callback in isolation."""

    async def test_task_is_released_and_nothing_opened_on_success(self) -> None:
        task = _finished_task()
        await asyncio.gather(task)
        fts._LOCAL_RUN_TASKS.add(task)
        factory = MagicMock()

        fts._supervise_local_run(
            task, execution_id=uuid.uuid4(), session_factory=factory
        )

        assert task not in fts._LOCAL_RUN_TASKS
        factory.assert_not_called()

    async def test_cancelled_task_is_logged_not_failed(self) -> None:
        task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        factory = MagicMock()
        execution_id = uuid.uuid4()

        with patch.object(fts, "logger") as logger:
            fts._supervise_local_run(
                task, execution_id=execution_id, session_factory=factory
            )

        factory.assert_not_called()
        logger.error.assert_not_called()
        assert "cancelled" in logger.warning.call_args.args[0]

    @pytest.mark.parametrize(
        "row",
        [
            SimpleNamespace(status="SUCCEEDED", agent_session_reference=None),
            SimpleNamespace(status="WAITING_FOR_HUMAN", agent_session_reference=None),
            SimpleNamespace(status="RUNNING", agent_session_reference="job-1"),
        ],
        ids=["terminal", "parked", "live-runtime"],
    )
    async def test_rows_that_are_not_stranded_are_not_overwritten(
        self, row: SimpleNamespace
    ) -> None:
        task = _finished_task(RuntimeError("late failure"))
        await asyncio.gather(task, return_exceptions=True)
        session = MagicMock(spec=Session)

        with patch.object(fts, "crud_flow_execution") as crud:
            crud.get.return_value = row
            fts._supervise_local_run(
                task, execution_id=uuid.uuid4(), session_factory=lambda: session
            )
            await _await_failure_writes()

        crud.update.assert_not_called()
        session.commit.assert_not_called()
        session.close.assert_called_once()

    async def test_failed_status_write_is_logged_and_rolled_back(self) -> None:
        task = _finished_task(RuntimeError("dispatch failed"))
        await asyncio.gather(task, return_exceptions=True)
        session = MagicMock(spec=Session)

        with (
            patch.object(fts, "crud_flow_execution") as crud,
            patch.object(fts, "logger") as logger,
        ):
            crud.get.return_value = SimpleNamespace(
                status="PENDING", agent_session_reference=None
            )
            crud.update.side_effect = RuntimeError("database unavailable")
            fts._supervise_local_run(
                task, execution_id=uuid.uuid4(), session_factory=lambda: session
            )
            await _await_failure_writes()

        session.rollback.assert_called_once()
        session.close.assert_called_once()
        assert (
            "Could not record the local dispatch failure"
            in logger.exception.call_args.args[0]
        )

    async def test_status_write_runs_off_the_event_loop_thread(self) -> None:
        task = _finished_task(RuntimeError("dispatch failed"))
        await asyncio.gather(task, return_exceptions=True)
        session = MagicMock(spec=Session)
        factory_threads: list[int] = []

        def _factory() -> Session:
            factory_threads.append(threading.get_ident())
            return session

        with patch.object(fts, "crud_flow_execution") as crud:
            crud.get.return_value = SimpleNamespace(
                status="PENDING", agent_session_reference=None
            )
            fts._supervise_local_run(
                task, execution_id=uuid.uuid4(), session_factory=_factory
            )
            # Nothing touched the database on the loop thread.
            assert factory_threads == []
            assert len(fts._LOCAL_RUN_FAILURE_WRITES) == 1
            await _await_failure_writes()

        assert len(factory_threads) == 1
        assert factory_threads[0] != threading.get_ident()
        crud.update.assert_called_once()
        session.commit.assert_called_once()
        assert not fts._LOCAL_RUN_FAILURE_WRITES

    async def test_crashed_failure_write_is_logged(self) -> None:
        task = _finished_task(RuntimeError("dispatch failed"))
        await asyncio.gather(task, return_exceptions=True)
        execution_id = uuid.uuid4()

        with (
            patch.object(
                fts,
                "_record_local_run_failure",
                side_effect=RuntimeError("write crashed"),
            ),
            patch.object(fts, "logger") as logger,
        ):
            fts._supervise_local_run(
                task, execution_id=execution_id, session_factory=MagicMock()
            )
            await _await_failure_writes()

        logged = [
            call
            for call in logger.error.call_args_list
            if "Could not record" in call.args[0]
        ]
        assert len(logged) == 1
        assert logged[0].args[1] == execution_id
        assert str(logged[0].kwargs["exc_info"]) == "write crashed"
        assert not fts._LOCAL_RUN_FAILURE_WRITES

    async def test_unschedulable_write_is_left_for_recovery(self) -> None:
        task = _finished_task(RuntimeError("dispatch failed"))
        await asyncio.gather(task, return_exceptions=True)
        closing_loop = MagicMock()
        closing_loop.create_task.side_effect = RuntimeError("loop is closed")
        factory = MagicMock()

        with (
            patch.object(task, "get_loop", return_value=closing_loop),
            patch.object(fts, "logger") as logger,
        ):
            fts._supervise_local_run(
                task, execution_id=uuid.uuid4(), session_factory=factory
            )

        factory.assert_not_called()
        assert not fts._LOCAL_RUN_FAILURE_WRITES
        assert "execution recovery" in logger.warning.call_args.args[0]
