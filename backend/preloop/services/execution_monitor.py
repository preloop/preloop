"""Background service to monitor and clean up stale flow executions."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from preloop.agents import AgentStatus
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.flow_execution import (
    FlowExecution,
    resolve_matrix_agent_selection,
)
from preloop.models.crud import crud_flow, crud_flow_execution

logger = logging.getLogger(__name__)


class ExecutionMonitor:
    """
    Background service that monitors active flow executions.

    Periodically checks for stale executions (e.g., RUNNING/PENDING for too long)
    and verifies if their containers are still alive. If not, updates status to FAILED.
    """

    def __init__(
        self,
        check_interval_seconds: int = 60,
        stale_threshold_minutes: int = 60,
    ):
        """
        Initialize execution monitor.

        Args:
            check_interval_seconds: How often to check for stale executions
            stale_threshold_minutes: How long an execution can be in RUNNING/PENDING
                                    before being considered potentially stale
        """
        self.check_interval = check_interval_seconds
        self.stale_threshold = timedelta(minutes=stale_threshold_minutes)
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start the monitoring background task."""
        if self._running:
            logger.warning("Execution monitor is already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            f"Execution monitor started (check_interval={self.check_interval}s, "
            f"stale_threshold={self.stale_threshold.total_seconds() / 60}m)"
        )

    async def stop(self):
        """Stop the monitoring background task."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected when stop() cancels the monitor loop task.
                pass

    async def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_stale_executions()
            except Exception as e:
                logger.error(f"Error in execution monitor loop: {e}", exc_info=True)

            # Wait before next check
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def _check_stale_executions(self):
        """Check for stale executions and update their status."""
        db: Session = next(get_db())
        try:
            # Find executions that are in active states using CRUD layer
            active_statuses = ["RUNNING", "PENDING", "INITIALIZING", "STARTING"]
            stale_executions = crud_flow_execution.get_by_statuses(
                db, statuses=active_statuses
            )

            if not stale_executions:
                return

            logger.debug(f"Found {len(stale_executions)} active executions to check")

            for execution in stale_executions:
                await self._check_execution(db, execution)

            db.commit()

        except Exception as e:
            logger.error(f"Error checking stale executions: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

    async def _check_execution(self, db: Session, execution: FlowExecution):
        """
        Check a single execution and update status if stale.

        Args:
            db: Database session
            execution: Flow execution to check
        """
        try:
            # Calculate how long the execution has been in current state
            now = datetime.now(timezone.utc)
            elapsed = now - execution.start_time.replace(tzinfo=timezone.utc)

            # Only check executions that have been running for a while
            if elapsed < self.stale_threshold:
                return

            # If no session reference, mark as failed (container never started)
            if not execution.agent_session_reference:
                logger.warning(
                    f"Execution {execution.id} has no session reference after "
                    f"{elapsed.total_seconds() / 60:.1f} minutes, marking as FAILED"
                )
                execution.status = "FAILED"
                execution.error_message = (
                    "Execution failed to start agent container within timeout"
                )
                execution.end_time = now
                return

            # Get the flow to determine agent type using CRUD layer
            flow = crud_flow.get(db, id=execution.flow_id)
            if not flow:
                logger.error(
                    f"Flow {execution.flow_id} not found for execution {execution.id}"
                )
                return

            # Create agent executor to check status. Matrix cells override the
            # flow's agent_type per execution; resolving through the shared
            # helper keeps the status check on the harness that actually runs
            # this cell (a foreign executor would mis-read the session).
            try:
                effective_agent_type, _ = resolve_matrix_agent_selection(
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
            except Exception as e:
                logger.error(
                    f"Failed to create agent executor for checking execution "
                    f"{execution.id}: {e}"
                )
                return

            # Check if container is still running
            try:
                status = await agent_executor.get_status(
                    execution.agent_session_reference
                )

                # Update execution status based on container status
                if status == AgentStatus.FAILED:
                    logger.warning(
                        f"Execution {execution.id} container failed, updating status"
                    )
                    execution.status = "FAILED"
                    execution.error_message = (
                        "Agent container failed or stopped unexpectedly"
                    )
                    execution.end_time = now

                elif status == AgentStatus.SUCCEEDED:
                    logger.info(
                        f"Execution {execution.id} container succeeded, updating status"
                    )
                    execution.status = "SUCCEEDED"
                    execution.end_time = now

                elif status == AgentStatus.STOPPED:
                    # Only update if not already in a terminal state
                    # Preserve STOPPED status if the user stopped the execution
                    if execution.status not in ("STOPPED", "FAILED", "SUCCEEDED"):
                        logger.info(
                            f"Execution {execution.id} container stopped, "
                            f"updating status to STOPPED"
                        )
                        execution.status = "STOPPED"
                        execution.error_message = "Execution was stopped"
                        execution.end_time = now
                    else:
                        logger.debug(
                            f"Execution {execution.id} already in terminal state "
                            f"({execution.status}), not updating"
                        )

                elif status in (AgentStatus.RUNNING, AgentStatus.STARTING):
                    # Container is still running, no action needed
                    logger.debug(
                        f"Execution {execution.id} container is still {status}"
                    )

            except Exception as e:
                # If we can't get status, container might be gone
                logger.warning(
                    f"Failed to get status for execution {execution.id} "
                    f"(session {execution.agent_session_reference}): {e}. "
                    f"Marking as FAILED."
                )
                execution.status = "FAILED"
                execution.error_message = (
                    f"Lost connection to agent container: {str(e)}"
                )
                execution.end_time = now

            finally:
                # Cleanup agent executor
                if hasattr(agent_executor, "cleanup"):
                    await agent_executor.cleanup()

        except Exception as e:
            logger.error(f"Error checking execution {execution.id}: {e}", exc_info=True)


# Global instance
_monitor_instance: ExecutionMonitor | None = None


def get_execution_monitor() -> ExecutionMonitor:
    """Get or create the global execution monitor instance."""
    global _monitor_instance
    if _monitor_instance is None:
        check_interval = int(os.getenv("EXECUTION_MONITOR_INTERVAL", "60"))
        stale_threshold = int(os.getenv("EXECUTION_STALE_THRESHOLD_MINUTES", "60"))
        _monitor_instance = ExecutionMonitor(
            check_interval_seconds=check_interval,
            stale_threshold_minutes=stale_threshold,
        )
    return _monitor_instance
