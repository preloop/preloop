"""A stopped run whose stop was never confirmed leaves recovery in one pass.

The recovery pass selects a terminal run only because its stop is still
unconfirmed. Finishing that stop must confirm it, must not rewrite the
stored outcome and must not notify the end of the run a second time.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.services import flow_execution_runner as runner
from preloop.services.execution_reaper import MAX_TERMINAL_REDISPATCHES
from preloop.services.execution_recovery import ExecutionRecoveryService
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

STOPPED_AT = datetime(2026, 1, 5, 12, 5)  # the column is naive UTC
ORIGINAL_ERROR = "Execution stopped by user request after 300 seconds."


class GoneRuntime:
    """An executor whose runtime no longer exists."""

    def __init__(self, stopped: bool = True) -> None:
        self.stopped = stopped
        self.stop = AsyncMock()
        self.aclose = AsyncMock()
        self.cleanup = AsyncMock()

    async def is_stopped(self, session_reference: str) -> bool:
        return self.stopped


@pytest.fixture
def flow(db_session: Session, test_user: Any) -> models.Flow:
    """An enabled flow owning the stopped runs."""
    row = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"stopped-loop-{uuid.uuid4().hex[:8]}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    db_session.commit()
    return row


def _stopped_run(
    db_session: Session, flow_id: Any, *, stop_source: str = "manual"
) -> models.FlowExecution:
    """A STOPPED run with an unconfirmed stop and a runtime reference."""
    execution = models.FlowExecution(
        flow_id=flow_id,
        status="STOPPED",
        start_time=STOPPED_AT - timedelta(seconds=300),
        end_time=STOPPED_AT,
        error_message=ORIGINAL_ERROR,
        result={"pr_url": "https://example.com/acme/app/pull/7"},
        agent_session_reference=f"agent-{uuid.uuid4().hex[:8]}",
        stop_requested_at=STOPPED_AT,
        stop_source=stop_source,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    return execution


def _candidate_ids(db_session: Session) -> set[Any]:
    return {
        row.id for row in crud_flow_execution.list_stale_or_unclaimed_active(db_session)
    }


async def _recovery_pass(db_session: Session, **executor_patches: Any) -> List[Any]:
    """Run one recovery pass and the worker task for each published resume."""
    published: List[Any] = []

    async def publish(execution_id: Any) -> bool:
        published.append(execution_id)
        return True

    with (
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_resume",
            side_effect=publish,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new_callable=AsyncMock,
        ),
    ):
        await ExecutionRecoveryService()._redispatch_stale_executions(
            db_session, stale_after_seconds=120
        )

    def sessions() -> Iterator[Session]:
        while True:
            yield db_session

    with (
        patch.object(runner, "get_db_session", side_effect=lambda: sessions()),
        patch.object(db_session, "close"),
        patch.object(runner, "get_nats_client", new_callable=AsyncMock),
        patch.multiple("preloop.agents", **executor_patches),
    ):
        for execution_id in published:
            await runner.claim_and_run_execution(str(execution_id), resume=True)
    return published


@pytest.mark.asyncio
async def test_stopped_run_with_gone_runtime_settles_in_one_pass(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id)
    assert execution.id in _candidate_ids(db_session)
    runtime = GoneRuntime()
    notify = AsyncMock()

    with patch.object(FlowExecutionOrchestrator, "_notify_terminal", notify):
        first = await _recovery_pass(
            db_session,
            create_executor_for_execution=MagicMock(return_value=runtime),
        )
        second = await _recovery_pass(
            db_session,
            create_executor_for_execution=MagicMock(return_value=runtime),
        )

    assert first == [execution.id]
    assert second == []
    assert execution.id not in _candidate_ids(db_session)
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is not None
    assert execution.status == "STOPPED"
    assert execution.end_time == STOPPED_AT
    assert execution.error_message == ORIGINAL_ERROR
    assert execution.result == {"pr_url": "https://example.com/acme/app/pull/7"}
    assert execution.orchestrator_worker_id is None
    assert notify.await_count == 0
    # The runtime was already gone: nothing needed stopping.
    runtime.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_run_with_live_runtime_is_stopped_then_confirmed(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id)
    execution.status = "FAILED"
    db_session.commit()
    runtime = GoneRuntime(stopped=False)

    async def stop(session_reference: str) -> None:
        runtime.stopped = True

    runtime.stop.side_effect = stop
    notify = AsyncMock()
    with patch.object(FlowExecutionOrchestrator, "_notify_terminal", notify):
        await _recovery_pass(
            db_session,
            create_executor_for_execution=MagicMock(return_value=runtime),
        )

    runtime.stop.assert_awaited_once_with(execution.agent_session_reference)
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is not None
    assert execution.status == "FAILED"
    assert execution.end_time == STOPPED_AT
    assert execution.error_message == ORIGINAL_ERROR
    assert notify.await_count == 0


@pytest.mark.asyncio
async def test_refused_executor_still_settles_a_pr_merged_stop(
    db_session: Session, flow: models.Flow
) -> None:
    """Launch admission checks must not keep a finished stop open."""
    execution = _stopped_run(db_session, flow.id, stop_source="pr_merged")
    runtime = GoneRuntime()
    plain = MagicMock(return_value=runtime)
    notify = AsyncMock()

    with patch.object(FlowExecutionOrchestrator, "_notify_terminal", notify):
        await _recovery_pass(
            db_session,
            create_executor_for_execution=MagicMock(
                side_effect=ValueError("host profile cannot run on hosted compute")
            ),
            create_agent_executor=plain,
        )

    plain.assert_called_once_with("openhands", {})
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is not None
    assert execution.end_time == STOPPED_AT
    assert execution.error_message == ORIGINAL_ERROR
    assert notify.await_count == 0
    assert execution.id not in _candidate_ids(db_session)


@pytest.mark.asyncio
async def test_unregistered_agent_type_falls_back_to_a_hosted_executor(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id, stop_source="pr_merged")
    runtime = GoneRuntime()
    calls: List[str] = []

    def plain(agent_type: str, config: dict) -> GoneRuntime:
        calls.append(agent_type)
        if agent_type != "codex":
            raise ValueError("Unsupported agent type")
        return runtime

    await _recovery_pass(
        db_session,
        create_executor_for_execution=MagicMock(side_effect=ValueError("refused")),
        create_agent_executor=MagicMock(side_effect=plain),
    )

    assert calls == ["openhands", "codex"]
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is not None


def test_claiming_a_terminal_run_keeps_its_redispatch_count(
    db_session: Session, flow: models.Flow
) -> None:
    stopped = _stopped_run(db_session, flow.id)
    stopped.redispatch_count = 3
    running = _stopped_run(db_session, flow.id)
    running.status = "RUNNING"
    running.redispatch_count = 3
    db_session.commit()

    for row in (stopped, running):
        assert crud_flow_execution.claim_execution(
            db_session, execution_id=row.id, worker_id="worker-a"
        )
    db_session.refresh(stopped)
    db_session.refresh(running)
    assert stopped.redispatch_count == 3
    assert running.redispatch_count == 0


@pytest.mark.asyncio
async def test_bound_settles_a_terminal_run_that_never_confirms(
    db_session: Session,
    flow: models.Flow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A case the resume path misses costs a few attempts, not forever."""
    execution = _stopped_run(db_session, flow.id)
    runtime = GoneRuntime(stopped=False)
    caplog.set_level(logging.ERROR, logger="preloop.services.execution_recovery")

    attempts = 0
    for _ in range(MAX_TERMINAL_REDISPATCHES + 2):
        # Step past the backoff so every pass is due.
        execution.last_redispatch_at = None
        db_session.commit()
        attempts += len(
            await _recovery_pass(
                db_session,
                create_executor_for_execution=MagicMock(return_value=runtime),
            )
        )

    assert attempts == MAX_TERMINAL_REDISPATCHES
    db_session.refresh(execution)
    # Nobody saw the runtime end, so the stop stays unconfirmed.
    assert execution.stop_confirmed_at is None
    assert execution.end_time == STOPPED_AT
    assert execution.error_message == ORIGINAL_ERROR
    assert execution.id not in _candidate_ids(db_session)
    # Leaving recovery is not proof the runtime ended; the row says so.
    assert "termination not verified" in execution.stop_reason
    settled = [r for r in caplog.records if str(execution.id) in r.getMessage()]
    assert len(settled) == 1
    assert settled[0].levelno == logging.ERROR


@pytest.mark.asyncio
async def test_bound_leaves_active_runs_to_the_backoff(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id)
    execution.status = "RUNNING"
    execution.stop_requested_at = None
    execution.stop_source = None
    execution.redispatch_count = MAX_TERMINAL_REDISPATCHES + 1
    db_session.commit()

    summary = SimpleNamespace(settled=0)
    candidates = crud_flow_execution.list_stale_or_unclaimed_active(db_session)
    kept = ExecutionRecoveryService()._settle_exhausted_terminal(
        db_session, candidates, summary
    )
    assert execution.id in {row.id for row in kept}
    assert summary.settled == 0
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is None


@pytest.mark.asyncio
async def test_resume_of_a_terminal_run_never_rewrites_or_notifies() -> None:
    """The direct resume entry point keeps the same guarantee."""
    orchestrator = MagicMock()
    orchestrator.db = MagicMock()
    orchestrator.flow = SimpleNamespace(agent_type="codex", agent_config={})
    orchestrator.execution_log = SimpleNamespace(
        id=uuid.uuid4(), status="STOPPED", flow_id=uuid.uuid4()
    )
    orchestrator._update_execution_log = AsyncMock()
    orchestrator._notify_terminal = AsyncMock()
    orchestrator._monitor_agent_execution = AsyncMock()
    runtime = GoneRuntime()

    with (
        patch(
            "preloop.agents.create_executor_for_execution",
            return_value=runtime,
        ),
        patch.object(runner.crud_flow_execution, "confirm_stop") as confirm,
    ):
        await runner.resume_existing_execution(orchestrator, "agent-ref")

    confirm.assert_called_once()
    orchestrator._monitor_agent_execution.assert_not_awaited()
    orchestrator._update_execution_log.assert_not_awaited()
    orchestrator._notify_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_flow_keeps_a_terminal_outcome(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id)
    with (
        patch.object(runner, "get_db_session", return_value=iter([db_session])),
        patch.object(db_session, "close"),
        patch.object(runner.crud_flow, "get", return_value=None),
        patch(
            "preloop.agents.create_agent_executor", return_value=GoneRuntime()
        ) as hosted,
    ):
        outcome = await runner.claim_and_run_execution(str(execution.id), resume=True)
    assert outcome["status"] == "missing_flow"
    hosted.assert_called_once_with("codex", {})
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is not None
    assert execution.status == "STOPPED"
    assert execution.end_time == STOPPED_AT
    assert execution.error_message == ORIGINAL_ERROR


@pytest.mark.asyncio
async def test_parked_run_is_not_treated_as_terminal() -> None:
    assert not runner._is_terminal(SimpleNamespace(status="WAITING_FOR_HUMAN"))
    assert runner._is_terminal(SimpleNamespace(status="CANCELLED"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference", ["control:agent-1:command-1", "control-consent:agent-1:c-1"]
)
async def test_refused_executor_never_inspects_a_control_reference_as_a_container(
    db_session: Session, flow: models.Flow, reference: str
) -> None:
    """A container lookup would call any non-container reference gone."""
    execution = _stopped_run(db_session, flow.id)
    execution.agent_session_reference = reference
    db_session.commit()
    plain = MagicMock(return_value=GoneRuntime())

    await _recovery_pass(
        db_session,
        create_executor_for_execution=MagicMock(side_effect=ValueError("refused")),
        create_agent_executor=plain,
    )

    plain.assert_not_called()
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is None


@pytest.mark.asyncio
async def test_failed_dispatch_of_a_terminal_run_still_counts_towards_the_bound(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id)
    with patch(
        "preloop.services.flow_execution_dispatcher.dispatch_resume",
        new_callable=AsyncMock,
        return_value=False,
    ):
        for _ in range(MAX_TERMINAL_REDISPATCHES + 1):
            execution.last_redispatch_at = None
            db_session.commit()
            await ExecutionRecoveryService()._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )
    db_session.refresh(execution)
    assert execution.redispatch_count == MAX_TERMINAL_REDISPATCHES
    assert execution.stop_confirmed_at is None
    assert "termination not verified" in execution.stop_reason
    assert execution.id not in _candidate_ids(db_session)


@pytest.mark.asyncio
async def test_confirmed_stop_retires_the_runtime_credentials(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id)
    with (
        patch(
            "preloop.services.flow_runtime_token.end_flow_execution_runtime_session"
        ) as end_session,
        patch(
            "preloop.services.flow_runtime_token.revoke_flow_runtime_tokens"
        ) as revoke,
    ):
        for _ in range(2):
            await _recovery_pass(
                db_session,
                create_executor_for_execution=MagicMock(return_value=GoneRuntime()),
            )
    assert end_session.call_count == 1
    assert end_session.call_args.kwargs["execution_id"] == execution.id
    assert end_session.call_args.kwargs["account_id"] == flow.account_id
    assert revoke.call_count == 1


@pytest.mark.asyncio
async def test_bound_retires_credentials_and_keeps_the_marker_of_a_long_reason(
    db_session: Session, flow: models.Flow
) -> None:
    execution = _stopped_run(db_session, flow.id)
    execution.stop_reason = "x" * 500
    execution.redispatch_count = MAX_TERMINAL_REDISPATCHES
    db_session.commit()
    with (
        patch(
            "preloop.services.flow_runtime_token.end_flow_execution_runtime_session"
        ) as end_session,
        patch(
            "preloop.services.flow_runtime_token.revoke_flow_runtime_tokens"
        ) as revoke,
    ):
        await _recovery_pass(
            db_session,
            create_executor_for_execution=MagicMock(return_value=GoneRuntime(False)),
        )
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is None
    assert execution.id not in _candidate_ids(db_session)
    assert len(execution.stop_reason) <= 500
    assert execution.stop_reason.endswith("operator inspection required")
    assert "termination not verified" in execution.stop_reason
    assert end_session.call_args.kwargs["execution_id"] == execution.id
    assert revoke.call_count == 1


def test_marker_only_takes_terminal_runs_out_of_recovery(
    db_session: Session, flow: models.Flow
) -> None:
    """A live run with the marker text is still recovered; claims agree."""
    stopped = _stopped_run(db_session, flow.id)
    crud_flow_execution.abandon_unverified_stop(
        db_session, execution_id=stopped.id, attempts=5
    )
    running = _stopped_run(db_session, flow.id)
    running.status = "RUNNING"
    running.stop_reason = stopped.stop_reason
    db_session.commit()
    db_session.refresh(stopped)
    assert stopped.stop_confirmed_at is None
    candidates = _candidate_ids(db_session)
    assert stopped.id not in candidates
    assert running.id in candidates
    assert (
        crud_flow_execution.claim_execution(
            db_session, execution_id=stopped.id, worker_id="worker-a"
        )
        is None
    )
