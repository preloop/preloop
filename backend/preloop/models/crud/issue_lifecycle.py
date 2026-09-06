"""Transactional lifecycle persistence and per-issue serialization."""

import asyncio
from contextlib import asynccontextmanager
from time import monotonic
from hashlib import sha256
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from preloop.models import models


class CRUDIssueLifecycle:
    """Serialize decisions through transaction locks, including first insert."""

    def get_issue(
        self, db: Session, *, account_id: UUID, issue_id: UUID
    ) -> models.Issue | None:
        """Resolve indirect tenant ownership explicitly rather than CRUDBase.get."""
        return db.scalar(
            select(models.Issue)
            .join(models.Tracker, models.Issue.tracker_id == models.Tracker.id)
            .where(models.Issue.id == issue_id, models.Tracker.account_id == account_id)
        )

    def get_project(
        self, db: Session, *, account_id: UUID, project_id: UUID
    ) -> models.Project | None:
        """Resolve project ownership through organization and tracker."""
        return db.scalar(
            select(models.Project)
            .join(
                models.Organization,
                models.Project.organization_id == models.Organization.id,
            )
            .join(models.Tracker, models.Organization.tracker_id == models.Tracker.id)
            .where(
                models.Project.id == project_id, models.Tracker.account_id == account_id
            )
        )

    @asynccontextmanager
    async def locked(
        self, db: Session, account_id: UUID, issue_id: UUID
    ) -> AsyncIterator[None]:
        """Keep provider effects and their local acknowledgment serialized.

        Provider effects MUST additionally be idempotent by operation marker;
        database rollback cannot undo a successful remote API call.
        """
        key = int.from_bytes(
            sha256(f"{account_id}:{issue_id}".encode()).digest()[:8], "big", signed=True
        )
        deadline = monotonic() + 10
        while not db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
        ):
            if monotonic() >= deadline:
                db.rollback()
                raise ValueError("lifecycle_operation_in_progress")
            # A blocking advisory lock would deadlock the event loop while the
            # lock owner is awaiting its provider response. Retry without
            # blocking the async request/worker that currently owns the lock.
            await asyncio.sleep(0.05)
        try:
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def get(
        self, db: Session, *, account_id: UUID, issue_id: UUID, kind: str, revision: str
    ) -> models.IssueLifecycle | None:
        """Load a tenant-scoped operation."""
        return db.scalar(
            select(models.IssueLifecycle).where(
                models.IssueLifecycle.account_id == account_id,
                models.IssueLifecycle.issue_id == issue_id,
                models.IssueLifecycle.kind == kind,
                models.IssueLifecycle.revision == revision,
            )
        )

    def list_for_issue(
        self, db: Session, *, account_id: UUID, issue_id: UUID
    ) -> list[models.IssueLifecycle]:
        """Return operation history for reconciliation."""
        return list(
            db.scalars(
                select(models.IssueLifecycle)
                .where(
                    models.IssueLifecycle.account_id == account_id,
                    models.IssueLifecycle.issue_id == issue_id,
                )
                .order_by(models.IssueLifecycle.created_at)
            )
        )

    def put(
        self,
        db: Session,
        *,
        account_id: UUID,
        issue_id: UUID,
        kind: str,
        revision: str,
        state: str,
        data: dict[str, Any],
    ) -> models.IssueLifecycle:
        """Upsert while the caller holds the issue lock; do not commit."""
        row = self.get(
            db, account_id=account_id, issue_id=issue_id, kind=kind, revision=revision
        )
        if row is None:
            row = models.IssueLifecycle(
                account_id=account_id, issue_id=issue_id, kind=kind, revision=revision
            )
            db.add(row)
        row.state, row.data = state, data
        db.flush()
        return row

    def create_execution(
        self,
        db: Session,
        *,
        row: models.IssueLifecycle,
        flow_id: UUID,
        event: dict[str, Any],
    ) -> models.FlowExecution:
        """Atomically attach one fresh conversation to an audit operation."""
        if row.execution_id:
            execution = db.get(models.FlowExecution, row.execution_id)
            if execution is None:
                raise ValueError("lifecycle_execution_missing")
            return execution
        execution = models.FlowExecution(
            flow_id=flow_id, status="PENDING", trigger_event_details=event
        )
        db.add(execution)
        db.flush()
        row.execution_id = execution.id
        db.flush()
        return execution


crud_issue_lifecycle = CRUDIssueLifecycle()
