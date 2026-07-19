"""Async session-optimization jobs: submit, execute, poll, and recover.

The LLM-backed optimization pass
(:meth:`SessionOptimizationService.get_account_session_optimization_suggestions`)
is synchronous end-to-end — its gateway call blocks for the whole model
round-trip — so it must NEVER run on the event loop. This module runs it on a
small dedicated thread pool and tracks each run as an
:class:`~preloop.models.models.optimization_job.OptimizationJob` row the
console polls, replacing the long-blocking inline request.

Design points (per the approved Lane B design):

* Module-level bounded ``ThreadPoolExecutor(max_workers=3)`` — a hard cap on
  concurrent model passes per API process.
* Each job opens its OWN DB session inside the worker thread and holds it for
  the job's duration (releasing it around the LLM call is explicitly
  deferred).
* Status transitions are atomic conditional UPDATEs; a heartbeat ticker
  thread keeps ``heartbeat_at`` fresh while the model call is in flight so
  the recovery sweep can tell a slow job from a dead one.
* Failures store only :data:`USER_FACING_JOB_ERROR` on the row; diagnostic
  detail goes to server logs.
* The recovery sweep (startup + every ~2 minutes) fails stuck-pending and
  dead-running jobs with the same retriable copy and prunes finished jobs
  past retention.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_optimization_job,
    crud_runtime_session,
    crud_user,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.optimization_job import (
    ACTIVE_JOB_STATUSES,
    OptimizationJob,
    OptimizationJobStatus,
)
from preloop.schemas.gateway_usage import (
    RuntimeSessionOptimizationRequest,
    RuntimeSessionOptimizationResponse,
)
from preloop.services.session_optimization import SessionOptimizationService

logger = logging.getLogger(__name__)

#: The only failure copy ever stored on (and shown for) a failed job. The
#: analysis is read-only, so "Nothing was changed." is always true.
USER_FACING_JOB_ERROR = "The model request didn't complete. Nothing was changed."

#: Worker liveness signal cadence while the (blocking) model call runs.
JOB_HEARTBEAT_INTERVAL_SECONDS = 60

#: Recovery sweep cadence.
JOB_SWEEP_INTERVAL_SECONDS = 120

#: A pending job nobody claimed within this window is considered lost.
JOB_STUCK_PENDING_MINUTES = 10

#: A running job silent for this long is considered dead.
JOB_STALE_HEARTBEAT_MINUTES = 5

#: Finished jobs are pruned after this many days.
JOB_RETENTION_DAYS = 14

# Bounded pool for the blocking optimization passes. Module-level so every
# code path (API workers, tests) shares the same cap.
_job_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="optimize-job")

# Strong references to in-flight futures so submitted work is never garbage
# collected mid-run; entries remove themselves on completion.
_job_futures: dict[uuid.UUID, Future] = {}
_job_futures_lock = threading.Lock()


def run_session_optimization(
    db: Session,
    *,
    account: Any,
    runtime_session_id: str,
    request: RuntimeSessionOptimizationRequest,
    current_user: Any,
    budget_enforcer: Any = None,
) -> RuntimeSessionOptimizationResponse:
    """Run one synchronous optimization pass (the shared worker function).

    This is the single entry point both the legacy inline endpoint and the
    async job worker call, so the two paths can never drift semantically.

    Args:
        db: Database session (held for the duration of the pass).
        account: Owning account.
        runtime_session_id: Runtime session to analyze.
        request: Analysis scope/model request.
        current_user: User the analysis runs as.
        budget_enforcer: Optional deployment budget enforcer.

    Returns:
        The generated (or cached) optimization response.
    """
    return SessionOptimizationService(db).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=runtime_session_id,
        request=request,
        current_user=current_user,
        budget_enforcer=budget_enforcer,
    )


def submit_session_optimization_job(
    db: Session,
    *,
    account: Any,
    current_user: Any,
    runtime_session_id: str,
    request: RuntimeSessionOptimizationRequest,
    budget_enforcer: Any = None,
) -> OptimizationJob:
    """Submit (or converge on) the async analysis job for one session.

    Idempotent by design: if an active (pending/running) job already exists
    for this account/session pair, that job is returned and NO new work is
    queued — a double-click must not spend the model budget twice.

    Args:
        db: Request-scoped database session.
        account: Owning account.
        current_user: Submitting user (the analysis runs as them).
        runtime_session_id: Runtime session to analyze.
        request: Analysis scope/model request.
        budget_enforcer: Optional deployment budget enforcer, captured for
            the worker thread.

    Returns:
        The active job for this session (existing or newly created).

    Raises:
        HTTPException: 404 when the session does not belong to the account.
    """
    session_row = crud_runtime_session.get(
        db, id=runtime_session_id, account_id=str(account.id)
    )
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime session not found",
        )
    existing = crud_optimization_job.get_active_for_session(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
    )
    if existing is not None:
        return existing
    job = crud_optimization_job.create_pending(
        db,
        account_id=account.id,
        runtime_session_id=runtime_session_id,
    )
    # A create that raced another submit returns the OTHER caller's active
    # job, which is already dispatched — never dispatch it twice.
    if job.status == OptimizationJobStatus.PENDING and not _is_dispatched(job.id):
        _dispatch_job(
            job.id,
            account_id=account.id,
            user_id=current_user.id,
            runtime_session_id=runtime_session_id,
            request=request,
            budget_enforcer=budget_enforcer,
        )
    return job


def _is_dispatched(job_id: uuid.UUID) -> bool:
    """Whether a future is already tracked for this job."""
    with _job_futures_lock:
        return job_id in _job_futures


def _dispatch_job(
    job_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    runtime_session_id: str,
    request: RuntimeSessionOptimizationRequest,
    budget_enforcer: Any,
) -> None:
    """Queue one job on the worker pool, keeping a strong future reference."""
    future = _job_executor.submit(
        _execute_job,
        job_id,
        account_id=account_id,
        user_id=user_id,
        runtime_session_id=runtime_session_id,
        request=request,
        budget_enforcer=budget_enforcer,
    )
    with _job_futures_lock:
        _job_futures[job_id] = future

    def _discard(_future: Future) -> None:
        with _job_futures_lock:
            _job_futures.pop(job_id, None)

    future.add_done_callback(_discard)


def _open_worker_session() -> Session:
    """Open a fresh DB session for a worker/heartbeat thread."""
    return next(get_db_session())


def _touch_heartbeat(job_id: uuid.UUID) -> None:
    """Record worker liveness using a short-lived session of its own."""
    db = _open_worker_session()
    try:
        crud_optimization_job.touch_heartbeat(db, job_id=job_id)
    except Exception:
        logger.warning(
            "Optimization job %s heartbeat update failed", job_id, exc_info=True
        )
    finally:
        db.close()


def _execute_job(
    job_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    user_id: uuid.UUID,
    runtime_session_id: str,
    request: RuntimeSessionOptimizationRequest,
    budget_enforcer: Any = None,
    db: Optional[Session] = None,
) -> None:
    """Worker-thread body for one job: claim, run, record the outcome.

    Args:
        job_id: Job row to execute.
        account_id: Owning account id (re-fetched in the worker session).
        user_id: Submitting user id (re-fetched in the worker session).
        runtime_session_id: Runtime session to analyze.
        request: Analysis scope/model request.
        budget_enforcer: Optional deployment budget enforcer.
        db: Test seam — inject a session instead of opening one. When
            injected, the caller owns the session and no heartbeat ticker
            thread is started.
    """
    owns_session = db is None
    if db is None:
        db = _open_worker_session()
    heartbeat_stop: Optional[threading.Event] = None
    try:
        # Atomic claim: only one worker (or sweep) wins this transition. A
        # lost claim means the job was already failed/cancelled — do nothing.
        claimed = crud_optimization_job.transition(
            db,
            job_id=job_id,
            from_statuses=(OptimizationJobStatus.PENDING,),
            to_status=OptimizationJobStatus.RUNNING,
        )
        if not claimed:
            logger.info(
                "Optimization job %s no longer pending; skipping execution", job_id
            )
            return

        if owns_session:
            stop_event = threading.Event()
            heartbeat_stop = stop_event

            def _beat() -> None:
                while not stop_event.wait(JOB_HEARTBEAT_INTERVAL_SECONDS):
                    _touch_heartbeat(job_id)

            threading.Thread(
                target=_beat,
                name=f"optimize-job-heartbeat-{job_id}",
                daemon=True,
            ).start()

        account = crud_account.get(db, id=account_id)
        user = crud_user.get(db, id=user_id)
        if account is None or user is None:
            raise RuntimeError(
                f"Account {account_id} or user {user_id} vanished before "
                f"optimization job {job_id} ran"
            )
        response = run_session_optimization(
            db,
            account=account,
            runtime_session_id=runtime_session_id,
            request=request,
            current_user=user,
            budget_enforcer=budget_enforcer,
        )
        finished = crud_optimization_job.transition(
            db,
            job_id=job_id,
            from_statuses=(OptimizationJobStatus.RUNNING,),
            to_status=OptimizationJobStatus.SUCCEEDED,
            result=response.model_dump(mode="json"),
        )
        if not finished:
            # The sweep declared the job dead while a (very) slow pass was
            # still in flight; the result stays in the cache table, so a
            # retried job returns it instantly.
            logger.warning(
                "Optimization job %s finished after being marked stale", job_id
            )
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        # Row gets the stable user-facing copy; the log line keeps the detail.
        logger.warning("Optimization job %s failed: %s", job_id, exc, exc_info=True)
        try:
            # The failed pass may have left the session mid-transaction; the
            # failure-recording UPDATE needs a clean one.
            db.rollback()
            crud_optimization_job.transition(
                db,
                job_id=job_id,
                from_statuses=ACTIVE_JOB_STATUSES,
                to_status=OptimizationJobStatus.FAILED,
                error=USER_FACING_JOB_ERROR,
            )
        except Exception:
            logger.error(
                "Could not record failure for optimization job %s",
                job_id,
                exc_info=True,
            )
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if owns_session:
            db.close()


def shutdown_job_executor() -> None:
    """Abandon in-flight jobs at process shutdown.

    ``wait=False`` on purpose: a model pass can take minutes and must not
    hold up shutdown. Abandoned rows are picked up by the recovery sweep
    after restart (stuck pending / stale heartbeat) and failed with the
    retriable user-facing copy.
    """
    _job_executor.shutdown(wait=False, cancel_futures=True)
    with _job_futures_lock:
        _job_futures.clear()


def run_optimization_job_sweep(db: Session) -> dict[str, int]:
    """Fail abandoned jobs and prune finished ones past retention.

    Args:
        db: Database session.

    Returns:
        Counts: ``{"failed": ..., "pruned": ...}``.
    """
    failed = crud_optimization_job.fail_stale(
        db,
        error=USER_FACING_JOB_ERROR,
        pending_older_than_minutes=JOB_STUCK_PENDING_MINUTES,
        heartbeat_older_than_minutes=JOB_STALE_HEARTBEAT_MINUTES,
    )
    pruned = crud_optimization_job.prune_finished(
        db, older_than_days=JOB_RETENTION_DAYS
    )
    if failed or pruned:
        logger.info(
            "Optimization job sweep: failed %s stale, pruned %s finished",
            failed,
            pruned,
        )
    return {"failed": failed, "pruned": pruned}


class OptimizationJobSweeper:
    """Periodic asyncio recovery sweep for optimization jobs.

    Modeled on :class:`preloop.services.execution_monitor.ExecutionMonitor`:
    started from the app lifespan, runs one sweep immediately (recovering
    jobs abandoned by the previous process) and then every
    ``check_interval`` seconds. The DB work is synchronous CRUD, so each pass
    runs in a thread to keep the event loop responsive.
    """

    def __init__(self, check_interval_seconds: int = JOB_SWEEP_INTERVAL_SECONDS):
        self.check_interval = check_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the sweep background task."""
        if self._running:
            logger.warning("Optimization job sweeper is already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info(
            "Optimization job sweeper started (check_interval=%ss)",
            self.check_interval,
        )

    async def stop(self) -> None:
        """Stop the sweep background task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected when stop() cancels the sweep loop task.
                pass

    async def _sweep_loop(self) -> None:
        """Run the sweep now, then on every interval tick."""
        while self._running:
            try:
                await asyncio.to_thread(self._sweep_once)
            except Exception:
                logger.error("Error in optimization job sweep", exc_info=True)
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    @staticmethod
    def _sweep_once() -> None:
        """One synchronous sweep pass with its own session."""
        db = _open_worker_session()
        try:
            run_optimization_job_sweep(db)
        finally:
            db.close()


# Global singleton instance
_sweeper_instance: OptimizationJobSweeper | None = None


def get_optimization_job_sweeper() -> OptimizationJobSweeper:
    """Get or create the global optimization job sweeper."""
    global _sweeper_instance
    if _sweeper_instance is None:
        _sweeper_instance = OptimizationJobSweeper()
    return _sweeper_instance
