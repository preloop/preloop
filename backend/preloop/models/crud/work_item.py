"""CRUD for work items, their pull requests and Delivery Report assessments."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.work_item import ASSESSMENT_VERDICTS, WORK_ITEM_STATES

from .base import CRUDBase

# Every allowed (from, to) move. ``merged``, ``no_change`` and ``closed`` are
# terminal; ``pr_open`` returns to ``open`` when a PR closes unmerged and
# ``ready`` returns to ``pr_open`` when a new push invalidates readiness.
WORK_ITEM_TRANSITIONS: list[tuple[str, str]] = [
    ("open", "pr_open"),
    ("open", "no_change"),
    ("open", "closed"),
    ("pr_open", "open"),
    ("pr_open", "ready"),
    ("pr_open", "merged"),
    ("pr_open", "closed"),
    ("ready", "pr_open"),
    ("ready", "merged"),
    ("ready", "closed"),
]


class InvalidWorkItemTransitionError(ValueError):
    """A state change the work item state machine does not allow."""

    def __init__(self, current: str, target: str) -> None:
        """Record the rejected move.

        Args:
            current: State the work item is in.
            target: State that was requested.
        """
        super().__init__(f"work item cannot move from {current!r} to {target!r}")
        self.current = current
        self.target = target


class CRUDWorkItem(CRUDBase[models.WorkItem]):
    """Create work items and move them through their states."""

    def open(
        self,
        db: Session,
        *,
        account_id: UUID,
        source: str,
        issue_id: Optional[UUID] = None,
        flow_execution_id: Optional[UUID] = None,
        flow_thread_id: Optional[UUID] = None,
        expected_artifact: Optional[str] = None,
        responsible_user_id: Optional[UUID] = None,
    ) -> models.WorkItem:
        """Create a work item in the ``open`` state.

        Args:
            db: Database session.
            account_id: Owning account.
            source: Where the work came from (flow, vendor agent, tracker).
            issue_id: Tracker issue the work addresses, if known.
            flow_execution_id: Run that produced the work, if any.
            flow_thread_id: Implementation thread, if any.
            expected_artifact: What the work should produce.
            responsible_user_id: Human accountable for the work.

        Returns:
            The flushed work item.
        """
        item = models.WorkItem(
            account_id=account_id,
            source=source,
            issue_id=issue_id,
            flow_execution_id=flow_execution_id,
            flow_thread_id=flow_thread_id,
            expected_artifact=expected_artifact,
            responsible_user_id=responsible_user_id,
            state="open",
        )
        db.add(item)
        db.flush()
        return item

    def transition(
        self,
        db: Session,
        *,
        item: models.WorkItem,
        state: str,
        closed_reason: Optional[str] = None,
        closed_by: Optional[str] = None,
    ) -> models.WorkItem:
        """Move a work item to ``state`` if the state machine allows it.

        Args:
            db: Database session.
            item: Work item to move.
            state: Target state.
            closed_reason: Why it closed; only recorded for ``closed``.
            closed_by: Actor that closed it; only recorded for ``closed``.

        Returns:
            The flushed work item.

        Raises:
            ValueError: ``state`` is not a work item state.
            InvalidWorkItemTransitionError: The move is not in
                ``WORK_ITEM_TRANSITIONS``.
        """
        if state not in WORK_ITEM_STATES:
            raise ValueError(f"unknown work item state {state!r}")
        if (item.state, state) not in WORK_ITEM_TRANSITIONS:
            raise InvalidWorkItemTransitionError(item.state, state)
        item.state = state
        if state == "closed":
            item.closed_reason = closed_reason
            item.closed_by = closed_by
        db.flush()
        return item

    def mark_pr_open(self, db: Session, *, item: models.WorkItem) -> models.WorkItem:
        """Record that a pull request is open for the work."""
        return self.transition(db, item=item, state="pr_open")

    def reopen(self, db: Session, *, item: models.WorkItem) -> models.WorkItem:
        """Return to ``open`` after the pull request closed unmerged."""
        return self.transition(db, item=item, state="open")

    def mark_ready(self, db: Session, *, item: models.WorkItem) -> models.WorkItem:
        """Record that the pull request is ready to merge."""
        return self.transition(db, item=item, state="ready")

    def mark_merged(self, db: Session, *, item: models.WorkItem) -> models.WorkItem:
        """Record that the work landed."""
        return self.transition(db, item=item, state="merged")

    def mark_no_change(self, db: Session, *, item: models.WorkItem) -> models.WorkItem:
        """Record that the run finished without producing a change."""
        return self.transition(db, item=item, state="no_change")

    def close(
        self,
        db: Session,
        *,
        item: models.WorkItem,
        reason: str,
        actor: str,
    ) -> models.WorkItem:
        """Close the work item without landing it.

        Args:
            db: Database session.
            item: Work item to close.
            reason: Why it closed.
            actor: Who closed it.

        Returns:
            The flushed work item.
        """
        return self.transition(
            db, item=item, state="closed", closed_reason=reason, closed_by=actor
        )

    def record_reconciliation(
        self,
        db: Session,
        *,
        item: models.WorkItem,
        error: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> models.WorkItem:
        """Stamp a reconciliation pass and its error, clearing a stale one.

        Args:
            db: Database session.
            item: Work item that was reconciled.
            error: Error from the pass, or None when it succeeded.
            at: Time of the pass; defaults to now.

        Returns:
            The flushed work item.
        """
        item.last_reconciled_at = at or datetime.now(timezone.utc)
        item.last_error = error
        db.flush()
        return item

    def list_by_state(
        self, db: Session, *, account_id: UUID, states: list[str]
    ) -> list[models.WorkItem]:
        """Return the account's work items in any of ``states``.

        Args:
            db: Database session.
            account_id: Owning account.
            states: States to include.

        Returns:
            Matching work items, oldest first.
        """
        return list(
            db.scalars(
                select(models.WorkItem)
                .where(
                    models.WorkItem.account_id == account_id,
                    models.WorkItem.state.in_(states),
                )
                .order_by(models.WorkItem.created_at, models.WorkItem.id)
            )
        )

    def get_for_thread(
        self, db: Session, *, account_id: UUID, flow_thread_id: UUID
    ) -> list[models.WorkItem]:
        """Return the account's work items bound to one flow thread."""
        return list(
            db.scalars(
                select(models.WorkItem).where(
                    models.WorkItem.account_id == account_id,
                    models.WorkItem.flow_thread_id == flow_thread_id,
                )
            )
        )


class CRUDWorkItemPR(CRUDBase[models.WorkItemPR]):
    """Record what the forge reports for a work item's pull requests."""

    def upsert(
        self,
        db: Session,
        *,
        item: models.WorkItem,
        repo: str,
        pr_number: int,
        state: str,
        **fields: Any,
    ) -> models.WorkItemPR:
        """Create or update the row for ``repo#pr_number`` on ``item``.

        Args:
            db: Database session.
            item: Owning work item.
            repo: Repository path; stored lower-cased.
            pr_number: Pull/merge request number.
            state: Forge state (open, closed, merged).
            **fields: Other ``WorkItemPR`` columns to set.

        Returns:
            The flushed row.
        """
        repo = repo.lower()
        row = db.scalar(
            select(models.WorkItemPR).where(
                models.WorkItemPR.work_item_id == item.id,
                models.WorkItemPR.repo == repo,
                models.WorkItemPR.pr_number == pr_number,
            )
        )
        if row is None:
            row = models.WorkItemPR(
                account_id=item.account_id,
                work_item_id=item.id,
                repo=repo,
                pr_number=pr_number,
                state=state,
            )
            db.add(row)
        row.state = state
        for name, value in fields.items():
            if not hasattr(models.WorkItemPR, name):
                raise ValueError(f"unknown work item PR field {name!r}")
            setattr(row, name, value)
        db.flush()
        return row

    def list_for_work_item(
        self, db: Session, *, work_item_id: UUID
    ) -> list[models.WorkItemPR]:
        """Return a work item's pull requests, oldest first."""
        return list(
            db.scalars(
                select(models.WorkItemPR)
                .where(models.WorkItemPR.work_item_id == work_item_id)
                .order_by(models.WorkItemPR.created_at, models.WorkItemPR.id)
            )
        )


class CRUDAssessment(CRUDBase[models.Assessment]):
    """Write Delivery Report assessments."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID,
        purpose_verdict: str,
        work_item: Optional[models.WorkItem] = None,
        report_run_id: Optional[UUID] = None,
        risk_flags: Optional[list[Any]] = None,
        evidence: Optional[dict[str, Any]] = None,
        cost: Optional[Decimal] = None,
    ) -> models.Assessment:
        """Write an assessment and make it the work item's latest.

        Args:
            db: Database session.
            account_id: Owning account.
            purpose_verdict: One of ``ASSESSMENT_VERDICTS``.
            work_item: Assessed work item, if the change has one.
            report_run_id: Delivery Report execution that wrote it.
            risk_flags: Likely regression classes.
            evidence: Supporting evidence.
            cost: What the assessment cost, in USD.

        Returns:
            The flushed assessment.

        Raises:
            ValueError: Unknown verdict, or a work item from another account.
        """
        if purpose_verdict not in ASSESSMENT_VERDICTS:
            raise ValueError(f"unknown purpose verdict {purpose_verdict!r}")
        if work_item is not None and work_item.account_id != account_id:
            raise ValueError("work item belongs to another account")
        row = models.Assessment(
            account_id=account_id,
            work_item_id=work_item.id if work_item is not None else None,
            report_run_id=report_run_id,
            purpose_verdict=purpose_verdict,
            risk_flags=risk_flags or [],
            evidence=evidence or {},
            cost=cost,
        )
        db.add(row)
        db.flush()
        if work_item is not None:
            work_item.latest_assessment_id = row.id
            db.flush()
        return row


crud_work_item = CRUDWorkItem(models.WorkItem)
crud_work_item_pr = CRUDWorkItemPR(models.WorkItemPR)
crud_assessment = CRUDAssessment(models.Assessment)
