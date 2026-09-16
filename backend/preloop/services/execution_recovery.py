"""Service for recovering orphaned flow executions after pod restarts."""

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional, Union

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_execution
from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_RUNNER_ERROR,
    derive_failure_category,
)
from preloop.sync.services.event_bus import get_nats_client
from .flow_orchestrator import FlowExecutionOrchestrator

logger = logging.getLogger(__name__)

#: A pass asks this before publishing anything: "can the worker pool take
#: more work right now?". True/False, or None when the caller cannot tell
#: (boot recovery, tests, a NATS hiccup), in which case the pass proceeds.
CapacityProbe = Callable[[], Union[Optional[bool], Awaitable[Optional[bool]]]]


def _utcnow() -> datetime:
    """Wall clock for the reaper, in one place so tests can fake it."""
    return datetime.now(timezone.utc)


def _exception_message(exc: BaseException) -> str:
    """Return a useful message for exceptions whose str() is empty."""
    return str(exc) or exc.__class__.__name__


def _retire_runtime_credentials(db: Session, execution: models.FlowExecution) -> None:
    """Close the runtime session and revoke the runtime tokens of an execution.

    Recovery decides an execution is over without ever running its
    orchestrator's teardown, so it has to retire the credentials itself.
    Otherwise the gateway token of a run that recovery just marked FAILED keeps
    authenticating until its two-hour expiry.
    """
    if execution is None:
        return
    account_id = getattr(getattr(execution, "flow", None), "account_id", None)
    if account_id is None:
        return
    from datetime import datetime, timezone

    from preloop.services.flow_runtime_token import (
        end_flow_execution_runtime_session,
        revoke_flow_runtime_tokens,
    )

    end_flow_execution_runtime_session(
        db,
        account_id=account_id,
        execution_id=execution.id,
        ended_at=datetime.now(timezone.utc),
    )
    revoke_flow_runtime_tokens(
        db,
        account_id=account_id,
        execution_id=execution.id,
    )


class ExecutionRecoveryService:
    """Recovers and resumes monitoring for orphaned flow executions."""

    def __init__(self):
        self.recovery_tasks: List[asyncio.Task] = []
        self.shutdown_event = asyncio.Event()

    async def recover_orphaned_executions(
        self,
        db: Session,
        *,
        capacity_probe: Optional[CapacityProbe] = None,
    ) -> int:
        """
        Find and resume monitoring for executions that were running when pod restarted.

        When ``FLOW_EXECUTION_WORKER_ENABLED`` is true, re-publishes JetStream
        tasks instead of starting orchestrators in-process. That pass runs
        under a lease, so with N replicas exactly one of them re-dispatches
        per interval instead of all N publishing the same work.

        Args:
            db: Database session
            capacity_probe: Optional "does the worker pool have a free slot?"
                callable. When it answers False, executions that still need a
                container are left queued instead of being republished into a
                queue nobody can drain.

        Returns:
            Number of executions recovered
        """
        from preloop.services.flow_execution_dispatcher import (
            claim_stale_after_seconds,
            flow_execution_worker_enabled,
            get_orchestrator_worker_id,
        )

        if flow_execution_worker_enabled():
            with crud_flow_execution.stale_claim_reaper_lease(
                db, holder=get_orchestrator_worker_id()
            ) as leased:
                if not leased:
                    # Debug, not info: the loser skips twice a minute per
                    # replica, and repeated identical lines are half of what
                    # made the original storm unreadable.
                    logger.debug(
                        "Stale-claim reaper: another worker holds the lease, "
                        "skipping this pass"
                    )
                    return 0
                return await self._redispatch_stale_executions(
                    db,
                    stale_after_seconds=claim_stale_after_seconds(),
                    capacity_probe=capacity_probe,
                )

        logger.info("Checking for orphaned flow executions to recover...")

        # Find all executions that are in RUNNING/STARTING/INITIALIZING/PENDING state
        running_statuses = ["RUNNING", "STARTING", "INITIALIZING", "PENDING"]

        orphaned_executions = []
        for status in running_statuses:
            executions = crud_flow_execution.get_multi(
                db,
                skip=0,
                limit=1000,  # Reasonable limit
                status=status,
            )
            orphaned_executions.extend(executions)

        if not orphaned_executions:
            logger.info("No orphaned executions found")
            return 0

        logger.info(
            f"Found {len(orphaned_executions)} orphaned execution(s) to recover"
        )

        # Get NATS client
        try:
            nats_client = await get_nats_client()
        except Exception as e:
            logger.error(f"Failed to connect to NATS for recovery: {e}")
            nats_client = None

        recovered_count = 0
        for execution in orphaned_executions:
            try:
                await self._resume_execution_monitoring(db, execution, nats_client)
                recovered_count += 1
            except Exception as e:
                logger.error(
                    f"Failed to recover execution {execution.id}: {e}",
                    exc_info=True,
                )

        logger.info(
            f"Successfully recovered {recovered_count}/{len(orphaned_executions)} executions"
        )
        return recovered_count

    async def _pool_has_capacity(
        self,
        capacity_probe: Optional[CapacityProbe],
    ) -> Optional[bool]:
        """Ask the caller whether the worker pool can take more work.

        Returns None when there is no probe or the probe failed: an unknown
        answer must not stop the deploy-handoff safety net, it only stops the
        optimisation.
        """
        if capacity_probe is None:
            return None
        try:
            answer = capacity_probe()
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception as exc:  # noqa: BLE001 - a probe never fails a pass
            logger.warning(
                "Worker-pool capacity probe failed (%s); running the pass anyway",
                _exception_message(exc),
            )
            return None
        return None if answer is None else bool(answer)

    def _admissible_candidates(
        self,
        db: Session,
        candidates: List[models.FlowExecution],
        *,
        stale_after_seconds: int,
        has_capacity: Optional[bool] = None,
        summary: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> List[models.FlowExecution]:
        """Drop the candidates republishing would only churn.

        Three filters, in this order:

        * per-execution backoff: an execution nobody claims is republished on
          a growing delay (30s, 60s, 2m, ... up to the cap) instead of on
          every pass. The counter and the timestamp live on the row, so the
          schedule is the same for every replica rather than per process;
        * worker-pool capacity: when the pool has no free slot, an execution
          that still needs a container is left queued. Publishing into a
          queue nobody can drain is pure load;
        * per-account admission: an execution whose account is already at its
          hosted concurrency cap is left alone. Publishing it would have a
          worker fetch it, refuse the claim and nak it, which is exactly the
          storm the cap is supposed to end. Work a private runner already
          holds does not count towards that cap.

        The last two filters apply only to unstarted PENDING rows. Anything
        with a live agent session is re-dispatched whatever the cap or the
        capacity says: an unmonitored container is worse than being one over
        a limit.
        """
        from preloop.services.execution_concurrency import account_running_cap
        from preloop.services.execution_reaper import (
            ReaperPassSummary,
            is_redispatch_due,
        )

        counts = summary if summary is not None else ReaperPassSummary()
        moment = now or _utcnow()
        admitted = crud_flow_execution.count_admitted_by_account(
            db, stale_after_seconds=stale_after_seconds, hosted_only=True
        )
        planned: dict[Any, int] = {}
        caps: dict[Any, int] = {}
        keep: List[models.FlowExecution] = []

        for execution in candidates:
            execution_id = str(execution.id)
            attempts = int(getattr(execution, "redispatch_count", 0) or 0)
            if not is_redispatch_due(
                attempts=attempts,
                last_redispatch_at=getattr(execution, "last_redispatch_at", None),
                now=moment,
            ):
                counts.skipped_backoff += 1
                logger.debug(
                    "Not re-dispatching %s: %s attempt(s) already, still inside "
                    "its backoff",
                    execution_id,
                    attempts,
                )
                continue

            needs_admission = (
                execution.status == "PENDING" and not execution.agent_session_reference
            )
            if needs_admission and has_capacity is False:
                counts.skipped_no_capacity += 1
                logger.debug(
                    "Not re-dispatching %s: no free worker slot to run it",
                    execution_id,
                )
                continue

            account_id = getattr(getattr(execution, "flow", None), "account_id", None)
            if needs_admission and account_id is not None:
                if account_id not in caps:
                    account = db.get(models.Account, account_id)
                    caps[account_id] = account_running_cap(account)
                in_flight = admitted.get(account_id, 0) + planned.get(account_id, 0)
                if in_flight >= caps[account_id]:
                    counts.skipped_account_cap += 1
                    logger.debug(
                        "Not re-dispatching %s: account %s is at its "
                        "concurrency cap (%s/%s); it stays PENDING",
                        execution_id,
                        account_id,
                        in_flight,
                        caps[account_id],
                    )
                    continue
                planned[account_id] = planned.get(account_id, 0) + 1

            keep.append(execution)

        return keep

    async def _redispatch_stale_executions(
        self,
        db: Session,
        *,
        stale_after_seconds: int,
        capacity_probe: Optional[CapacityProbe] = None,
    ) -> int:
        """Re-publish execute/resume tasks for unclaimed or stale-claim executions.

        One summary line per pass, never one line per candidate: a storm has
        to read as a number.
        """
        from preloop.services.execution_reaper import ReaperPassSummary
        from preloop.services.flow_execution_dispatcher import (
            dispatch_execute,
            dispatch_resume,
        )

        logger.debug(
            "Worker-mode recovery: listing stale/unclaimed active flow executions "
            "(stale_after=%ss)...",
            stale_after_seconds,
        )
        has_capacity = await self._pool_has_capacity(capacity_probe)
        candidates = crud_flow_execution.list_stale_or_unclaimed_active(
            db,
            stale_after_seconds=stale_after_seconds,
            limit=500,
        )
        summary = ReaperPassSummary(candidates=len(candidates))
        if not candidates:
            logger.debug("No stale/unclaimed executions to re-dispatch")
            logger.info(summary.as_log_line())
            return 0

        now = _utcnow()
        admissible = self._admissible_candidates(
            db,
            candidates,
            stale_after_seconds=stale_after_seconds,
            has_capacity=has_capacity,
            summary=summary,
            now=now,
        )
        if not admissible:
            logger.info(summary.as_log_line())
            return 0

        semaphore = asyncio.Semaphore(20)

        async def dispatch_candidate(execution: models.FlowExecution) -> bool:
            try:
                async with semaphore:
                    if execution.agent_session_reference:
                        ok = await dispatch_resume(execution.id)
                    else:
                        ok = await dispatch_execute(execution.id)
                    if ok:
                        logger.debug(
                            "Re-dispatched %s for execution %s (status=%s)",
                            "resume_flow_execution"
                            if execution.agent_session_reference
                            else "execute_flow",
                            execution.id,
                            execution.status,
                        )
                    return bool(ok)
            except Exception as e:
                logger.error(
                    "Failed to re-dispatch execution %s: %s",
                    execution.id,
                    e,
                    exc_info=True,
                )
                return False

        results = await asyncio.gather(
            *(dispatch_candidate(execution) for execution in admissible)
        )
        published = [
            execution.id
            for execution, ok in zip(admissible, results, strict=False)
            if ok
        ]
        summary.redispatched = len(published)
        summary.failed = len(admissible) - len(published)

        if published:
            # Count the publish against each row before the next pass reads
            # it, so the backoff holds across workers and across restarts.
            crud_flow_execution.record_redispatch(db, execution_ids=published, now=now)

        logger.info(summary.as_log_line())
        return summary.redispatched

    async def _resume_execution_monitoring(
        self,
        db: Session,
        execution: models.FlowExecution,
        nats_client,
    ):
        """Resume monitoring for a specific execution (legacy in-process path)."""
        logger.info(
            f"Resuming monitoring for execution {execution.id} "
            f"(status: {execution.status}, agent_session: {execution.agent_session_reference})"
        )

        from datetime import datetime, timezone
        from preloop.models.schemas.flow_execution import FlowExecutionUpdate

        # If execution doesn't have an agent session yet, it failed during startup
        if not execution.agent_session_reference:
            logger.warning(
                f"Execution {execution.id} has no agent session - marking as FAILED"
            )
            update_data = FlowExecutionUpdate(
                status="FAILED",
                error_message="Execution interrupted during startup (pod restart)",
                # The run never reached a provider or an agent: the runtime
                # lost it, so it is a runner failure, not an agent one.
                failure_category=FAILURE_CATEGORY_RUNNER_ERROR,
                end_time=datetime.now(timezone.utc),
            )
            crud_flow_execution.update(db, db_obj=execution, obj_in=update_data)
            db.commit()
            _retire_runtime_credentials(db, execution)
            return

        # Check if the container/job still exists before trying to monitor
        # This avoids blocking on containers that were cleaned up during deploy
        try:
            from preloop.models.models.flow_execution import (
                resolve_execution_agent_selection,
            )

            flow = execution.flow
            if flow:
                # Matrix cells override the flow's agent_type per execution;
                # a recovered cell must be probed with its own harness.
                effective_agent_type, _ = resolve_execution_agent_selection(
                    execution.trigger_event_details,
                    flow_agent_type=flow.agent_type,
                )
                from preloop.agents import create_executor_for_execution

                agent_executor = create_executor_for_execution(
                    effective_agent_type,
                    {"agent_config": flow.agent_config or {}},
                    flow=flow,
                    execution=execution,
                    db=db,
                    execution_context={
                        "trigger_event_data": execution.trigger_event_details,
                        "account_id": flow.account_id,
                    },
                )
                try:
                    status = await agent_executor.get_status(
                        execution.agent_session_reference
                    )

                    # If container is already in terminal state, update DB and skip monitoring
                    from preloop.agents.base import AgentStatus

                    if status in (AgentStatus.FAILED, AgentStatus.STOPPED):
                        logger.warning(
                            f"Execution {execution.id} container is {status.value} - marking as FAILED"
                        )
                        update_data = FlowExecutionUpdate(
                            status="FAILED",
                            error_message=f"Container was {status.value} on recovery (likely cleaned up during deploy)",
                            failure_category=FAILURE_CATEGORY_RUNNER_ERROR,
                            end_time=datetime.now(timezone.utc),
                        )
                        crud_flow_execution.update(
                            db, db_obj=execution, obj_in=update_data
                        )
                        db.commit()
                        _retire_runtime_credentials(db, execution)
                        return
                    elif status == AgentStatus.SUCCEEDED:
                        logger.info(
                            f"Execution {execution.id} container succeeded - marking as SUCCEEDED"
                        )
                        update_data = FlowExecutionUpdate(
                            status="SUCCEEDED",
                            end_time=datetime.now(timezone.utc),
                        )
                        crud_flow_execution.update(
                            db, db_obj=execution, obj_in=update_data
                        )
                        db.commit()
                        _retire_runtime_credentials(db, execution)
                        return
                    # Container is RUNNING/STARTING - proceed with monitoring below
                finally:
                    close_client = getattr(agent_executor, "aclose", None)
                    if callable(close_client):
                        await close_client()

        except Exception as check_error:
            check_error_message = _exception_message(check_error)
            logger.warning(
                f"Error checking container status for {execution.id}: {check_error_message}. "
                "Marking as FAILED to avoid hanging."
            )
            update_data = FlowExecutionUpdate(
                status="FAILED",
                error_message=f"Container check failed during recovery: {check_error_message}",
                failure_category=FAILURE_CATEGORY_RUNNER_ERROR,
                end_time=datetime.now(timezone.utc),
            )
            crud_flow_execution.update(db, db_obj=execution, obj_in=update_data)
            db.commit()
            # Status check failed: agent liveness is unknown. Leave the
            # runtime token alone (it lapses on expiry) rather than revoking
            # a credential an agent may still be using.
            return

        # Container is still running - create orchestrator and resume monitoring
        orchestrator = FlowExecutionOrchestrator(
            db,
            flow_id=execution.flow_id,
            trigger_event_data=execution.trigger_event_details or {},
            nats_client=nats_client,
        )
        orchestrator.execution_log = execution
        orchestrator._is_recovered = (
            True  # Mark as recovered to skip external API calls
        )

        # Resume monitoring as a background task
        task = asyncio.create_task(
            self._resume_monitoring_task(
                orchestrator, execution.agent_session_reference
            )
        )
        self.recovery_tasks.append(task)

    async def _resume_monitoring_task(self, orchestrator, session_reference: str):
        """Background task that resumes monitoring an agent execution."""
        db = None
        try:
            logger.info(f"Resumed monitoring task for session {session_reference}")

            # Get a fresh database session for this task
            # (the session passed to orchestrator during recovery was closed)
            from preloop.models.db.session import get_db_session

            db = next(get_db_session())

            # Update orchestrator to use the fresh session
            orchestrator.db = db

            # Re-fetch execution_log from new session (old one is detached)
            execution_id = orchestrator.execution_log.id
            orchestrator.execution_log = crud_flow_execution.get(db, id=execution_id)

            # Re-fetch flow from new session for account_id access in _publish_update
            orchestrator.flow = orchestrator.execution_log.flow

            try:
                from preloop.services.flow_execution_runner import (
                    resume_existing_execution,
                )

                await resume_existing_execution(orchestrator, session_reference)
            finally:
                db.close()

        except Exception as e:
            error_message = _exception_message(e)
            logger.error(
                f"Error in resumed monitoring task for {session_reference}: {error_message}",
                exc_info=True,
            )
            # Mark as failed - need a fresh session since the one above was closed
            failure_db = None
            try:
                from datetime import datetime, timezone
                from preloop.models.schemas.flow_execution import FlowExecutionUpdate
                from preloop.models.db.session import get_db_session

                failure_db = next(get_db_session())

                # Re-fetch execution_log from failure session
                execution_log = crud_flow_execution.get(
                    failure_db, id=orchestrator.execution_log.id
                )

                update_data = FlowExecutionUpdate(
                    status="FAILED",
                    error_message=f"Resumed monitoring failed: {str(e)}",
                    failure_category=derive_failure_category(
                        status="FAILED",
                        error_message=error_message,
                        exception=e,
                    ),
                    end_time=datetime.now(timezone.utc),
                )
                crud_flow_execution.update(
                    failure_db,
                    db_obj=execution_log,
                    obj_in=update_data,
                )
                failure_db.commit()
                _retire_runtime_credentials(failure_db, execution_log)
            except Exception as update_error:
                logger.error(f"Failed to mark execution as failed: {update_error}")
            finally:
                if failure_db is not None:
                    failure_db.close()

    async def wait_for_completion(self, timeout: int = 300):
        """
        Wait for all recovery tasks to complete before shutdown.

        Args:
            timeout: Maximum time to wait in seconds (default 5 minutes)
        """
        if not self.recovery_tasks:
            logger.info("No recovery tasks to wait for")
            return

        logger.info(
            f"Waiting for {len(self.recovery_tasks)} recovery tasks to complete..."
        )

        try:
            await asyncio.wait_for(
                asyncio.gather(*self.recovery_tasks, return_exceptions=True),
                timeout=timeout,
            )
            logger.info("All recovery tasks completed")
        except asyncio.TimeoutError:
            logger.warning(
                f"Recovery tasks did not complete within {timeout}s - proceeding with shutdown"
            )
            # Cancel remaining tasks
            for task in self.recovery_tasks:
                if not task.done():
                    task.cancel()


# Global singleton instance
_recovery_service = None


def get_recovery_service() -> ExecutionRecoveryService:
    """Get the global recovery service instance."""
    global _recovery_service
    if _recovery_service is None:
        _recovery_service = ExecutionRecoveryService()
    return _recovery_service
