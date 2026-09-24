"""Atomic inbox and execution reservations; backend services never query tables."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "ABORTED", "STOPPED"}

# Statuses a launch may have when it died before storing a session.
# ``sessionless_retry`` in the feedback service uses the same set: a status
# added on only one side would revive a thread the scheduler stopped on purpose.
SESSIONLESS_RETRY_STATUSES = frozenset({"FAILED", "TIMED_OUT"})


class CRUDFlowFeedback:
    """Transaction boundaries for subscriptions, inbox receipts and dispatch."""

    def release_read(self, db: Session) -> None:
        """End a materialized read before asynchronous provider I/O."""
        db.commit()

    def rollback(self, db: Session) -> None:
        db.rollback()

    def unregistered_publications(self, db: Session) -> list[models.FlowExecution]:
        execution = models.FlowExecution
        bound = exists().where(
            models.FlowThread.flow_id == execution.flow_id,
            models.FlowThread.pr_url == execution.result["pr_url"].astext,
        )
        return list(
            db.execute(
                select(execution)
                .join(models.Flow, models.Flow.id == execution.flow_id)
                .where(
                    models.Flow.agent_config["feedback"]["enabled"]
                    .as_boolean()
                    .is_(True),
                    execution.result["pr_url"].astext.isnot(None),
                    execution.trigger_event_details["_session_thread_id"].astext.isnot(
                        None
                    ),
                    ~bound,
                )
                .order_by(execution.created_at.desc())
                .limit(20)
            ).scalars()
        )

    def register(
        self, db: Session, *, values: dict[str, Any], commit: bool = True
    ) -> models.FlowThread:
        values = {"id": uuid.uuid4(), **values}
        statement = insert(models.FlowThread).values(**values)
        db.execute(
            statement.on_conflict_do_nothing(constraint="uq_flow_thread_binding")
        )
        query = select(models.FlowThread).filter_by(
            **{
                key: values[key]
                for key in (
                    "account_id",
                    "flow_id",
                    "tracker_id",
                    "repository_id",
                    "pr_number",
                )
            }
        )
        thread = db.execute(query).scalar_one()
        if commit:
            db.commit()
        return thread

    def find(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        tracker_id: uuid.UUID,
        repository_id: str,
        pr_number: str | None = None,
    ) -> list[models.FlowThread]:
        query = select(models.FlowThread).filter_by(
            account_id=account_id, tracker_id=tracker_id, repository_id=repository_id
        )
        if pr_number is not None:
            query = query.filter_by(pr_number=pr_number)
        return list(db.execute(query).scalars())

    def owned_thread(
        self,
        db: Session,
        *,
        thread_id: uuid.UUID,
        account_id: uuid.UUID,
        flow_id: uuid.UUID,
    ) -> models.FlowThread | None:
        return db.execute(
            select(models.FlowThread).filter_by(
                id=thread_id, account_id=account_id, flow_id=flow_id
            )
        ).scalar_one_or_none()

    def ingest(
        self,
        db: Session,
        *,
        thread_id: uuid.UUID,
        events: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        for event in events:
            statement = insert(models.FlowFeedback).values(
                id=uuid.uuid4(), thread_id=thread_id, **event
            )
            db.execute(
                statement.on_conflict_do_update(
                    constraint="uq_flow_feedback_delivery",
                    set_={
                        "head_sha": statement.excluded.head_sha,
                        "payload": statement.excluded.payload,
                    },
                    where=models.FlowFeedback.consumed_by.is_(None),
                )
            )
        # Preserve the existing due time; repeated delivery cannot starve dispatch.
        db.commit()

    def stopped_for_no_progress(
        self, db: Session, *, limit: int = 20
    ) -> list[models.FlowThread]:
        """Threads a launch failure stopped before the agent stored a session.

        Only rows that can actually be revived are returned. A genuine
        no-progress stop never leaves ``stopped``, and ``claim_due`` will not
        advance it, so a window over every stop lets permanent stops crowd
        out the sessionless ones this scan exists to retry.

        Args:
            db: Database session.
            limit: Maximum threads to return, oldest due first.

        Returns:
            Stopped threads whose latest execution failed or timed out
            without a stored session id or checkpoint artifact.
        """
        execution = models.FlowExecution
        session_id = execution.cli_session["session_id"].astext
        artifact = execution.cli_session["artifact_reference"].astext
        # Match execution_has_native_session: a missing or empty value is not
        # a session. A stored artifact object still counts.
        no_session = and_(
            or_(session_id.is_(None), session_id == ""),
            or_(artifact.is_(None), artifact == ""),
        )
        return list(
            db.execute(
                select(models.FlowThread)
                .join(execution, execution.id == models.FlowThread.latest_execution_id)
                .where(
                    models.FlowThread.state == "stopped",
                    models.FlowThread.stop_reason == "no_progress",
                    execution.status.in_(tuple(SESSIONLESS_RETRY_STATUSES)),
                    no_session,
                )
                .order_by(models.FlowThread.due_at)
                .limit(limit)
            ).scalars()
        )

    def revive(self, db: Session, thread_id: uuid.UUID, *, now: datetime) -> bool:
        """Return one no-progress stop to the scheduler.

        The update is conditional on the stopped/no-progress state, so two
        replicas cannot interleave a revive with a later state change.
        Commits only this transition.

        Args:
            db: Database session.
            thread_id: Thread to return to the waiting queue.
            now: Due time the scheduler should pick the thread up at.

        Returns:
            True when this call changed the row.
        """
        result = db.execute(
            update(models.FlowThread)
            .where(
                models.FlowThread.id == thread_id,
                models.FlowThread.state == "stopped",
                models.FlowThread.stop_reason == "no_progress",
            )
            .values(state="waiting", stop_reason=None, no_progress=0, due_at=now)
        )
        db.commit()
        return result.rowcount > 0

    def claim_due(
        self, db: Session, *, now: datetime, limit: int = 20
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        query = (
            select(models.FlowThread)
            .where(
                models.FlowThread.due_at <= now,
                models.FlowThread.state.notin_(("closed", "stopped", "expired")),
                or_(
                    models.FlowThread.lease_until.is_(None),
                    models.FlowThread.lease_until <= now,
                ),
            )
            .order_by(models.FlowThread.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claims = []
        for thread in db.execute(query).scalars():
            thread.lease_token = uuid.uuid4()
            thread.lease_until = now + timedelta(seconds=120)
            claims.append((thread.id, thread.lease_token))
        db.commit()
        return claims

    def leased(
        self, db: Session, thread_id: uuid.UUID, token: uuid.UUID, *, lock: bool = False
    ) -> models.FlowThread | None:
        query = select(models.FlowThread).filter_by(id=thread_id, lease_token=token)
        if lock:
            query = query.with_for_update()
        return db.execute(query).scalar_one_or_none()

    def release_consumed(
        self, db: Session, thread_id: uuid.UUID, execution_id: uuid.UUID
    ) -> None:
        """Return reviews consumed by one execution to the pending inbox.

        Does not commit. The caller commits with the rest of the reconciliation.
        """
        db.execute(
            update(models.FlowFeedback)
            .where(
                models.FlowFeedback.thread_id == thread_id,
                models.FlowFeedback.consumed_by == execution_id,
            )
            .values(consumed_by=None)
        )
        db.flush()

    def pending(self, db: Session, thread_id: uuid.UUID) -> list[models.FlowFeedback]:
        return list(
            db.execute(
                select(models.FlowFeedback)
                .where(
                    models.FlowFeedback.thread_id == thread_id,
                    models.FlowFeedback.consumed_by.is_(None),
                )
                .order_by(models.FlowFeedback.created_at)
                .limit(200)
            ).scalars()
        )

    def acknowledge_observed(
        self,
        db: Session,
        thread: models.FlowThread,
        *,
        head_sha: str,
        present_keys: list[str],
    ) -> None:
        """A reconciliation consumes wakeups and obsolete heads, not new feedback."""
        query = select(models.FlowFeedback).where(
            models.FlowFeedback.thread_id == thread.id,
            models.FlowFeedback.consumed_by.is_(None),
            or_(
                models.FlowFeedback.kind == "signal",
                models.FlowFeedback.head_sha != head_sha,
                models.FlowFeedback.event_key.notin_(present_keys),
            ),
        )
        for receipt in db.execute(query).scalars():
            receipt.consumed_by = thread.latest_execution_id
        db.commit()

    def stop_active(
        self, db: Session, thread: models.FlowThread, *, reason: str, now: datetime
    ) -> uuid.UUID | None:
        """Persist cancellation before sending the best-effort live stop signal."""
        execution = (
            db.get(models.FlowExecution, thread.active_execution_id)
            if thread.active_execution_id
            else None
        )
        if execution is None or execution.status in TERMINAL:
            return None
        execution.status = "STOPPED"
        execution.error_message = reason
        execution.end_time = now
        if execution.runner_id:
            runner = db.get(models.FlowRunner, execution.runner_id)
            if runner is not None and runner.account_id == thread.account_id:
                assignment = runner.assignment_for(execution.id)
                if assignment is not None:
                    assignment.halt_requested = True
        db.commit()
        return execution.id

    def finish_active(self, db: Session, thread: models.FlowThread) -> bool:
        if thread.active_execution_id is None:
            return True
        execution = db.get(models.FlowExecution, thread.active_execution_id)
        if execution is None or execution.status not in TERMINAL:
            return False
        thread.latest_execution_id = execution.id
        thread.active_execution_id = None
        thread.cost += float(execution.estimated_cost or 0)
        return True

    def update(
        self,
        db: Session,
        thread_id: uuid.UUID,
        token: uuid.UUID,
        *,
        changes: dict[str, Any],
        now: datetime,
    ) -> bool:
        thread = self.leased(db, thread_id, token, lock=True)
        if thread is None:
            return False
        for key, value in changes.items():
            setattr(thread, key, value)
        execution = db.get(models.FlowExecution, thread.latest_execution_id)
        if execution is not None:
            pending_count = len(self.pending(db, thread.id))
            execution.result = {
                **(execution.result or {}),
                "continuation": {
                    "thread_id": str(thread.id),
                    "state": thread.state,
                    "stop_reason": thread.stop_reason,
                    "repair_turns": thread.turns,
                    "max_turns": int(thread.policy.get("max_turns", 5)),
                    "estimated_cost": thread.cost,
                    "pending_feedback": pending_count,
                    "head_sha": thread.head_sha,
                    "expires_at": thread.expires_at.isoformat(),
                },
            }
        thread.lease_until = None
        thread.lease_token = None
        thread.due_at = now + timedelta(seconds=30)
        db.commit()
        return True

    def sync_policy(
        self, db: Session, thread: models.FlowThread, policy: dict[str, Any]
    ) -> None:
        """Apply live policy without replenishing spent budgets or extending TTL."""
        thread.policy = dict(policy)
        original = thread.created_at.replace(tzinfo=None)
        thread.expires_at = min(
            thread.expires_at,
            original + timedelta(hours=int(policy.get("max_age_hours", 168))),
        )
        db.commit()

    def reserve(
        self,
        db: Session,
        thread_id: uuid.UUID,
        token: uuid.UUID,
        *,
        event_data: dict[str, Any],
        receipt_ids: list[uuid.UUID],
        head_sha: str,
        now: datetime,
        expected_policy: dict[str, Any] | None = None,
    ) -> models.FlowExecution | None:
        candidate = self.leased(db, thread_id, token)
        if candidate is None:
            db.rollback()
            return None
        # Parent before child matches flow deletion's FK cascade lock order.
        flow = db.execute(
            select(models.Flow)
            .where(models.Flow.id == candidate.flow_id)
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        ).scalar_one_or_none()
        thread = self.leased(db, thread_id, token, lock=True)
        if (
            thread is None
            or thread.lease_until is None
            or thread.lease_until < now
            or thread.active_execution_id
        ):
            db.rollback()
            return None
        # Re-read under a shared row lock so a concurrent policy edit cannot
        # commit between this authorization check and execution reservation.
        policy = (flow.agent_config or {}).get("feedback") if flow else None
        if (
            flow is None
            or flow.account_id != thread.account_id
            or not flow.is_enabled
            or not isinstance(policy, dict)
            or policy.get("enabled") is not True
            or (expected_policy is not None and policy != expected_policy)
            or now >= thread.expires_at
            or thread.turns >= int(policy.get("max_turns", 5))
            or thread.cost >= float(policy.get("max_cost", 100))
            or thread.no_progress >= int(policy.get("max_no_progress", 2))
        ):
            thread.lease_until = None
            thread.lease_token = None
            thread.due_at = now
            db.commit()
            return None
        # Record the publishing execution as the resume root for console
        # rollups. Prefer the prior turn's root when this is a later repair.
        details = dict(event_data)
        resume = dict(details.get("_resume") or {})
        if resume and thread.latest_execution_id is not None:
            prior = db.get(models.FlowExecution, thread.latest_execution_id)
            prior_resume = (
                (prior.trigger_event_details or {}).get("_resume")
                if prior is not None and isinstance(prior.trigger_event_details, dict)
                else None
            )
            prior_root = (
                prior_resume.get("resume_root")
                if isinstance(prior_resume, dict)
                else None
            )
            resume["resume_root"] = (
                str(prior_root) if prior_root else str(thread.latest_execution_id)
            )
            details["_resume"] = resume
        execution = models.FlowExecution(
            id=uuid.uuid4(),
            flow_id=thread.flow_id,
            status="PENDING",
            trigger_event_details=details,
        )
        db.add(execution)
        db.flush()
        thread.active_execution_id = execution.id
        thread.turns += 1
        thread.state = "repairing"
        thread.head_sha = head_sha
        thread.lease_until = None
        thread.lease_token = None
        thread.due_at = now + timedelta(seconds=30)
        # Reservation and receipts commit together. Dispatch is recoverable from
        # this same PENDING execution ID; no second execution is created on retry.
        for receipt in db.execute(
            select(models.FlowFeedback).where(
                models.FlowFeedback.thread_id == thread.id,
                models.FlowFeedback.id.in_(receipt_ids),
                models.FlowFeedback.consumed_by.is_(None),
            )
        ).scalars():
            receipt.consumed_by = execution.id
        db.commit()
        return execution


crud_flow_feedback = CRUDFlowFeedback()
