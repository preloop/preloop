"""Atomic inbox and execution reservations; backend services never query tables."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "ABORTED", "STOPPED"}


class CRUDFlowFeedback:
    """Transaction boundaries for subscriptions, inbox receipts and dispatch."""

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
                    ~bound,
                )
                .order_by(execution.created_at.desc())
                .limit(20)
            ).scalars()
        )

    def register(self, db: Session, *, values: dict[str, Any]) -> models.FlowThread:
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
                runner.halt_requested = True
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
    ) -> models.FlowExecution | None:
        thread = self.leased(db, thread_id, token, lock=True)
        if (
            thread is None
            or thread.lease_until is None
            or thread.lease_until < now
            or thread.active_execution_id
        ):
            db.rollback()
            return None
        execution = models.FlowExecution(
            id=uuid.uuid4(),
            flow_id=thread.flow_id,
            status="PENDING",
            trigger_event_details=event_data,
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
