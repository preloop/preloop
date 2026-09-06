"""Run a pre-created flow execution on a sync worker (or local fallback)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Set

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow_execution import FlowExecutionUpdate
from preloop.services.flow_execution_dispatcher import (
    claim_stale_after_seconds,
    dispatch_execute,
    dispatch_resume,
    get_orchestrator_worker_id,
)
from preloop.services.flow_failure_category import derive_failure_category
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.flow_pr_binding import merge_result_preserving_pr_binding
from preloop.services.kill_switch import flows_halted
from preloop.sync.services.event_bus import get_nats_client

logger = logging.getLogger(__name__)

AckCallback = Callable[[], Awaitable[None]]

# Executions this process currently holds a claim for (deploy drain).
_active_claimed_execution_ids: Set[str] = set()


def get_active_claimed_execution_ids() -> Set[str]:
    """Return a copy of execution ids claimed by this worker process."""
    return set(_active_claimed_execution_ids)


async def run_existing_execution(
    orchestrator: FlowExecutionOrchestrator,
) -> None:
    """Run orchestration for a pre-created ``execution_log`` (full lifecycle).

    Skips creating a new FlowExecution row; ``orchestrator.execution_log`` must
    already be set. Closes nothing — caller owns the DB session.
    """
    if orchestrator.execution_log is None:
        raise ValueError("execution_log must be set before run_existing_execution")
    await orchestrator.run()


async def resume_existing_execution(
    orchestrator: FlowExecutionOrchestrator,
    session_reference: str,
) -> None:
    """Resume monitoring for a recovered execution that already has an agent."""
    from datetime import datetime, timezone

    from preloop.agents import create_executor_for_execution

    if orchestrator.execution_log is None:
        raise ValueError("execution_log must be set before resume_existing_execution")

    orchestrator._is_recovered = True
    orchestrator._get_flow_details()

    flow = orchestrator.flow
    if flow is None:
        raise ValueError("Flow not found for resumed execution")

    # Matrix-aware: _get_flow_details() above resolved any per-cell agent_type
    # override into orchestrator.agent_type; a resumed matrix cell must be
    # monitored with its own harness, never the flow default.
    agent_executor = create_executor_for_execution(
        orchestrator.agent_type or flow.agent_type,
        flow.agent_config or {},
        flow=flow,
        execution=orchestrator.execution_log,
        db=orchestrator.db,
    )
    try:
        agent_result = await orchestrator._monitor_agent_execution(
            session_reference, agent_executor
        )
        final_status = agent_result.get("status", "FAILED")
        resume_category = derive_failure_category(
            status=final_status,
            error_message=agent_result.get("error_message"),
            failure_analysis=agent_result.get("failure_analysis"),
        )
        merged_result = merge_result_preserving_pr_binding(
            getattr(orchestrator.execution_log, "result", None),
            agent_result.get("result"),
        )
        await orchestrator._update_execution_log(
            status=final_status,
            model_output_summary=agent_result.get("output_summary"),
            error_message=agent_result.get("error_message"),
            # Same reasoning as the initial-run terminal update: the executor's
            # verdict over the full logs beats re-deriving from the summary.
            failure_category=resume_category,
            actions_taken_summary=agent_result.get("actions_taken"),
            mcp_usage_logs=agent_result.get("mcp_usage_logs"),
            result=merged_result,
            end_time=datetime.now(timezone.utc),
        )
        await orchestrator._notify_terminal(
            status=final_status,
            failure_category=resume_category,
            result=merged_result,
        )
        # The worker that finishes a run owns its teardown, even though it did
        # not mint the credential: close the runtime session and revoke every
        # runtime token of this execution. The worker that started the run left
        # both alive on purpose, because the agent was still using them.
        orchestrator._sync_runtime_session(ended_at=datetime.now(timezone.utc))
        orchestrator._revoke_execution_runtime_tokens()
        logger.info(
            "Resumed execution %s completed with status %s",
            orchestrator.execution_log.id,
            final_status,
        )
    finally:
        close_client = getattr(agent_executor, "aclose", None)
        if callable(close_client):
            try:
                await close_client()
            except Exception as close_error:  # noqa: BLE001 - best-effort close
                logger.warning(
                    "Error during agent aclose after resume: %s", close_error
                )
        cleanup = getattr(agent_executor, "cleanup", None)
        if callable(cleanup):
            try:
                await cleanup()
            except Exception as cleanup_error:  # noqa: BLE001 - best-effort cleanup
                logger.warning(
                    "Error during agent cleanup after resume: %s", cleanup_error
                )


async def _redispatch_after_interrupt(
    execution_id: str,
    *,
    session_reference: Optional[str],
) -> None:
    """Hand an interrupted active execution to another worker immediately."""
    try:
        if session_reference:
            await dispatch_resume(execution_id)
            logger.info(
                "Re-dispatched resume_flow_execution for interrupted %s",
                execution_id,
            )
        else:
            await dispatch_execute(execution_id)
            logger.info(
                "Re-dispatched execute_flow for interrupted %s",
                execution_id,
            )
    except Exception as exc:
        logger.error(
            "Failed to re-dispatch interrupted execution %s: %s",
            execution_id,
            exc,
            exc_info=True,
        )


async def claim_and_run_execution(
    execution_id: str,
    *,
    resume: bool = False,
    ack: Optional[AckCallback] = None,
) -> dict[str, Any]:
    """Claim a flow execution, ack JetStream, then run or resume orchestration.

    If ``resume`` is False but the row already has ``agent_session_reference``,
    upgrades to resume monitoring so deploy rediscovery cannot start a duplicate
    agent Job.

    On cancellation (deploy SIGTERM), releases the claim and immediately
    re-dispatches so a peer worker can adopt without waiting for lease expiry.

    Args:
        execution_id: Flow execution UUID string.
        resume: When True, resume monitoring an existing agent session.
        ack: Optional callback invoked after a successful claim (ack-after-claim).

    Returns:
        Status dict for worker logging.
    """
    worker_id = get_orchestrator_worker_id()
    stale_after = claim_stale_after_seconds()
    db = next(get_db_session())
    claimed = False
    interrupted = False
    session_reference: Optional[str] = None
    execution_id_str = str(execution_id)
    try:
        execution = crud_flow_execution.claim_execution(
            db,
            execution_id=execution_id,
            worker_id=worker_id,
            stale_after_seconds=stale_after,
        )
        if execution is None:
            logger.info(
                "Could not claim execution %s (already owned or not active); skipping",
                execution_id_str,
            )
            return {"status": "skipped", "execution_id": execution_id_str}

        claimed = True
        _active_claimed_execution_ids.add(execution_id_str)
        session_reference = execution.agent_session_reference
        if ack is not None:
            await ack()

        # A halt prevents launch, not recovery of the monitor that must stop
        # an existing runtime. Fresh admission is serialized again at dispatch.
        flow_row = crud_flow.get(db, id=execution.flow_id)
        if flow_row is None:
            crud_flow_execution.update(
                db,
                db_obj=execution,
                obj_in=FlowExecutionUpdate(
                    status="FAILED",
                    failure_category="runner_error",
                    error_message="Flow no longer exists; execution cannot continue",
                    end_time=datetime.now(timezone.utc),
                ),
            )
            db.commit()
            return {"status": "missing_flow", "execution_id": execution_id_str}
        if not session_reference:
            if crud_flow_execution.cancel_unstarted_stop(db, execution_id=execution_id):
                return {"status": "stopped", "execution_id": execution_id_str}
            if crud_flow_execution.get_stop_request(db, execution_id=execution_id):
                # A start may have created a runtime before failing to return a
                # reference. Never infer termination or start a replacement.
                crud_flow_execution.update(
                    db,
                    db_obj=execution,
                    obj_in=FlowExecutionUpdate(
                        status="FAILED",
                        failure_category="runner_error",
                        error_message="Stop unconfirmed: launch was requested but no runtime reference was recorded; operator inspection required",
                    ),
                )
                db.commit()
                return {"status": "stop_unconfirmed", "execution_id": execution_id_str}
            try:
                if flows_halted(db, flow_row.account_id):
                    return {"status": "halted", "execution_id": execution_id_str}
            except Exception:
                logger.exception(
                    "Halt state unavailable for execution %s", execution_id_str
                )
                return {"status": "halt_lookup_error", "execution_id": execution_id_str}

        nats_client = await get_nats_client()
        flow_id = execution.flow_id
        if isinstance(flow_id, str):
            flow_id = uuid.UUID(flow_id)

        orchestrator = FlowExecutionOrchestrator(
            db,
            flow_id=flow_id,
            trigger_event_data=execution.trigger_event_details or {},
            nats_client=nats_client,
        )
        orchestrator.execution_log = execution
        orchestrator._orchestrator_worker_id = worker_id

        # Never start a second agent when a session already exists.
        effective_resume = resume or bool(session_reference)
        if effective_resume and not resume and session_reference:
            logger.warning(
                "execute_flow for %s found existing agent session %s; "
                "upgrading to resume to avoid duplicate agents",
                execution_id_str,
                session_reference,
            )

        if effective_resume:
            if not session_reference:
                logger.warning(
                    "resume_flow_execution for %s has no agent session; "
                    "falling back to full execute",
                    execution_id_str,
                )
                await run_existing_execution(orchestrator)
            else:
                await resume_existing_execution(orchestrator, session_reference)
        else:
            await run_existing_execution(orchestrator)

        # Refresh session ref after run (may have been set during execute).
        if orchestrator.execution_log is not None:
            session_reference = orchestrator.execution_log.agent_session_reference

        return {
            "status": "completed",
            "execution_id": execution_id_str,
            "final_status": getattr(orchestrator.execution_log, "status", None),
        }
    except asyncio.CancelledError:
        interrupted = True
        logger.info(
            "Orchestration cancelled for execution %s (worker draining)",
            execution_id_str,
        )
        raise
    except Exception as exc:
        logger.error(
            "claim_and_run_execution failed for %s: %s",
            execution_id_str,
            exc,
            exc_info=True,
        )
        raise
    finally:
        _active_claimed_execution_ids.discard(execution_id_str)
        if claimed:
            try:
                crud_flow_execution.release_claim(
                    db, execution_id=execution_id, worker_id=worker_id
                )
            except Exception as release_error:
                logger.warning(
                    "Failed to release claim for %s: %s",
                    execution_id_str,
                    release_error,
                )
            if interrupted:
                await _redispatch_after_interrupt(
                    execution_id_str,
                    session_reference=session_reference,
                )
        try:
            db.close()
        except Exception:
            pass


async def evacuate_owned_claims(*, worker_id: Optional[str] = None) -> int:
    """Release and re-dispatch all active claims still owned by this worker.

    Used during SIGTERM drain after in-flight tasks are cancelled, to catch any
    claims that were not cleaned up by ``claim_and_run_execution`` finally.
    """
    owner = worker_id or get_orchestrator_worker_id()
    db = next(get_db_session())
    evacuated = 0
    try:
        owned = crud_flow_execution.list_claimed_by_worker(
            db, worker_id=owner, active_only=True
        )
        for execution in owned:
            execution_id = str(execution.id)
            session_reference = execution.agent_session_reference
            try:
                crud_flow_execution.release_claim(
                    db, execution_id=execution.id, worker_id=owner
                )
            except Exception as release_error:
                logger.warning(
                    "Evacuate: failed to release claim %s: %s",
                    execution_id,
                    release_error,
                )
                continue
            await _redispatch_after_interrupt(
                execution_id, session_reference=session_reference
            )
            evacuated += 1
        return evacuated
    finally:
        db.close()
