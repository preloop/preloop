"""CRUD operations for async session-optimization jobs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.optimization_job import (
    ACTIVE_JOB_STATUSES,
    FINISHED_JOB_STATUSES,
    OptimizationJob,
    OptimizationJobStatus,
)
from .base import CRUDBase


def _utcnow() -> datetime:
    """Naive UTC now, matching the DateTime columns used across the models."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CRUDOptimizationJob(CRUDBase[OptimizationJob]):
    """CRUD operations for optimization jobs."""

    def get_for_session(
        self,
        db: Session,
        *,
        job_id: Union[uuid.UUID, str],
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
    ) -> Optional[OptimizationJob]:
        """Return one job scoped to its owning account AND session.

        Both filters are mandatory so a job id can never be read across
        accounts or attached to the wrong session (callers 404 on ``None``).

        Args:
            db: Database session.
            job_id: Job id.
            account_id: Owning account id.
            runtime_session_id: Runtime session the job must belong to.

        Returns:
            The job row, or ``None`` when absent or out of scope.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.id == job_id,
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
            )
            .first()
        )

    def get_active_for_session(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
    ) -> Optional[OptimizationJob]:
        """Return the pending/running job for one session, if any.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.

        Returns:
            The active job row, or ``None``.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.runtime_session_id == runtime_session_id,
                self.model.status.in_(ACTIVE_JOB_STATUSES),
            )
            .order_by(self.model.created_at.desc())
            .first()
        )

    def create_pending(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        runtime_session_id: Union[uuid.UUID, str],
    ) -> OptimizationJob:
        """Create a pending job, converging on the active one under races.

        The partial unique index on active (account, session) pairs makes a
        concurrent double-submit an :class:`IntegrityError`; in that case the
        already-active job is returned so both callers poll the same run.

        Args:
            db: Database session.
            account_id: Owning account id.
            runtime_session_id: Runtime session id.

        Returns:
            The newly created pending job, or the already-active job.
        """
        job = OptimizationJob(
            account_id=account_id,
            runtime_session_id=runtime_session_id,
            status=OptimizationJobStatus.PENDING,
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = self.get_active_for_session(
                db,
                account_id=account_id,
                runtime_session_id=runtime_session_id,
            )
            if existing is None:
                raise
            return existing
        db.refresh(job)
        return job

    def transition(
        self,
        db: Session,
        *,
        job_id: Union[uuid.UUID, str],
        from_statuses: tuple[str, ...],
        to_status: str,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Atomically move one job between statuses.

        Implemented as a single conditional ``UPDATE ... WHERE status IN``
        so two workers (or a worker racing the recovery sweep) can never both
        claim the same transition.

        Args:
            db: Database session.
            job_id: Job to transition.
            from_statuses: Statuses the job must currently be in.
            to_status: Target status.
            result: Serialized response stored on success.
            error: User-facing message stored on failure.

        Returns:
            True when this call performed the transition.
        """
        now = _utcnow()
        values: dict[Any, Any] = {"status": to_status, "heartbeat_at": now}
        if to_status == OptimizationJobStatus.RUNNING:
            values["started_at"] = now
        if to_status in FINISHED_JOB_STATUSES:
            values["finished_at"] = now
            values["result"] = result
            values["error"] = error
        updated = (
            db.query(self.model)
            .filter(
                self.model.id == job_id,
                self.model.status.in_(from_statuses),
            )
            .update(values, synchronize_session=False)
        )
        db.commit()
        return bool(updated)

    def touch_heartbeat(self, db: Session, *, job_id: Union[uuid.UUID, str]) -> bool:
        """Record worker liveness for one running job.

        Args:
            db: Database session.
            job_id: Job whose heartbeat to update.

        Returns:
            True when the job was still running and was touched.
        """
        updated = (
            db.query(self.model)
            .filter(
                self.model.id == job_id,
                self.model.status == OptimizationJobStatus.RUNNING,
            )
            .update({"heartbeat_at": _utcnow()}, synchronize_session=False)
        )
        db.commit()
        return bool(updated)

    def fail_stale(
        self,
        db: Session,
        *,
        error: str,
        pending_older_than_minutes: int = 10,
        heartbeat_older_than_minutes: int = 5,
    ) -> int:
        """Fail abandoned jobs: stuck-pending or running with a dead heartbeat.

        Covers workers lost to restarts/crashes: a pending job no thread ever
        claimed, or a running job whose owner stopped heartbeating.

        Args:
            db: Database session.
            error: User-facing error stored on the failed jobs.
            pending_older_than_minutes: Age after which a pending job is stuck.
            heartbeat_older_than_minutes: Heartbeat silence after which a
                running job is considered dead.

        Returns:
            Number of jobs flipped to failed.
        """
        now = _utcnow()
        pending_cutoff = now - timedelta(minutes=pending_older_than_minutes)
        heartbeat_cutoff = now - timedelta(minutes=heartbeat_older_than_minutes)
        failed_values: dict[Any, Any] = {
            "status": OptimizationJobStatus.FAILED,
            "error": error,
            "finished_at": now,
        }
        stuck_pending = (
            db.query(self.model)
            .filter(
                self.model.status == OptimizationJobStatus.PENDING,
                self.model.created_at < pending_cutoff,
            )
            .update(failed_values, synchronize_session=False)
        )
        dead_running = (
            db.query(self.model)
            .filter(
                self.model.status == OptimizationJobStatus.RUNNING,
                # A running job always has a heartbeat (set on claim); treat a
                # NULL one as stale too rather than letting it hang forever.
                (self.model.heartbeat_at.is_(None))
                | (self.model.heartbeat_at < heartbeat_cutoff),
            )
            .update(failed_values, synchronize_session=False)
        )
        db.commit()
        return int(stuck_pending) + int(dead_running)

    def prune_finished(self, db: Session, *, older_than_days: int = 14) -> int:
        """Delete finished jobs past the retention window.

        Args:
            db: Database session.
            older_than_days: Retention window for succeeded/failed jobs.

        Returns:
            Number of jobs deleted.
        """
        cutoff = _utcnow() - timedelta(days=older_than_days)
        deleted = (
            db.query(self.model)
            .filter(
                self.model.status.in_(FINISHED_JOB_STATUSES),
                self.model.finished_at.isnot(None),
                self.model.finished_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(deleted)
