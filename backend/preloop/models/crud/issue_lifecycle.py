"""Transactional lifecycle persistence and per-issue serialization."""

import asyncio
from contextlib import asynccontextmanager
from time import monotonic
from hashlib import sha256
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import exists, or_, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from preloop.models import models


class CRUDIssueLifecycle:
    """Serialize decisions through transaction locks, including first insert."""

    def has_triage_execution(self, db: Session, *, execution_id: Any) -> bool:
        """Classify a trusted worker task without returning tenant data."""
        return (
            db.scalar(
                select(
                    exists().where(
                        models.IssueLifecycle.kind.in_(["triage", "triage_attempt"]),
                        models.IssueLifecycle.execution_id == execution_id,
                    )
                )
            )
            is True
        )

    def commit(self, db: Session) -> None:
        """Durably acknowledge a triage operation without releasing its lock."""
        db.commit()

    def triage_flow(
        self, db: Session, *, account_id: UUID, flow_id: UUID
    ) -> models.Flow | None:
        """Refresh policy-bearing saved flow configuration under the issue lock."""
        return db.scalar(
            select(models.Flow)
            .where(models.Flow.id == flow_id, models.Flow.account_id == account_id)
            .execution_options(populate_existing=True)
        )

    def rollback(self, db: Session) -> None:
        """Recover a failed snapshot transaction; prior intents stay durable."""
        db.rollback()

    def retry_triage(self, db: Session, *, row: models.IssueLifecycle) -> None:
        """Archive a failed attempt before binding the explicit replacement."""
        archived = self.put(
            db,
            account_id=row.account_id,
            issue_id=row.issue_id,
            kind="triage_attempt",
            revision=sha256(str(row.execution_id).encode()).hexdigest(),
            state=row.state,
            data=dict(row.data),
        )
        archived.execution_id = row.execution_id
        row.execution_id = None
        db.flush()

    @asynccontextmanager
    async def triage_locked(
        self, db: Session, account_id: UUID, issue_id: UUID
    ) -> AsyncIterator[None]:
        """Hold the issue lock across durable provider-intent commits.

        A dedicated checked-out connection owns the session lock. Committing
        the data Session cannot release it or move it to another connection.
        The key is shared with readiness's transaction lock.
        """
        key = int.from_bytes(
            sha256(f"{account_id}:{issue_id}".encode()).digest()[:8], "big", signed=True
        )
        bind = db.get_bind()
        connection = bind if isinstance(bind, Connection) else bind.connect()
        owned = connection is not bind
        acquired = False
        try:
            deadline = monotonic() + 10
            while not connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ):
                if monotonic() >= deadline:
                    raise ValueError("triage_operation_in_progress")
                await asyncio.sleep(0.05)
            acquired = True
            # Refresh ORM identity-map objects after another controller's commit.
            db.expire_all()
            yield
        except BaseException:
            db.rollback()
            raise
        finally:
            try:
                if acquired:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                    )
            except BaseException:
                # Never put a connection with an uncertain session lock back.
                connection.invalidate()
                raise
            finally:
                if owned:
                    connection.close()

    def triage_for_execution(
        self,
        db: Session,
        *,
        account_id: UUID,
        execution_id: UUID,
        include_attempts: bool = False,
    ) -> models.IssueLifecycle | None:
        """Read the server-owned triage binding, never an event's claimed row."""
        return db.scalar(
            select(models.IssueLifecycle).where(
                models.IssueLifecycle.account_id == account_id,
                models.IssueLifecycle.kind.in_(
                    ["triage", "triage_attempt"] if include_attempts else ["triage"]
                ),
                models.IssueLifecycle.execution_id == execution_id,
            )
        )

    def issue_target(
        self,
        db: Session,
        *,
        account_id: UUID,
        project_id: UUID,
        external_id: str | None,
        number: str | None,
    ) -> models.Issue | None:
        """Resolve a delivery inside its authenticated tenant/project only."""
        identifiers = []
        if external_id:
            identifiers.append(models.Issue.external_id == external_id)
        if number:
            identifiers.extend(
                [
                    models.Issue.key == number,
                    models.Issue.key.endswith("#" + number, autoescape=True),
                ]
            )
        if not identifiers:
            return None
        return db.scalars(
            select(models.Issue)
            .join(models.Tracker, models.Issue.tracker_id == models.Tracker.id)
            .where(
                models.Tracker.account_id == account_id,
                models.Issue.project_id == project_id,
                or_(*identifiers),
            )
        ).one_or_none()

    def triage_snapshot(
        self, db: Session, *, issue: models.Issue, values: dict[str, Any]
    ) -> None:
        """Flush an observed triage snapshot without releasing the issue lock."""
        for field in ("title", "description", "status", "last_updated_external"):
            if field in values:
                setattr(issue, field, values[field])
        if "labels" in values:
            issue.meta_data = {**(issue.meta_data or {}), "labels": values["labels"]}
        db.add(issue)
        db.flush()

    def triage_receipt(
        self, db: Session, *, issue: models.Issue, receipt: dict[str, Any]
    ) -> None:
        """Flush trusted suppression intent inside the controller transaction."""
        issue.meta_data = {**(issue.meta_data or {}), "preloop_triage": dict(receipt)}
        db.add(issue)
        db.flush()

    def get_issue(
        self, db: Session, *, account_id: UUID, issue_id: UUID
    ) -> models.Issue | None:
        """Resolve indirect tenant ownership explicitly rather than CRUDBase.get."""
        return db.scalar(
            select(models.Issue)
            .join(models.Tracker, models.Issue.tracker_id == models.Tracker.id)
            .where(models.Issue.id == issue_id, models.Tracker.account_id == account_id)
            .execution_options(populate_existing=True)
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
            .execution_options(populate_existing=True)
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

    def archive_pickup(self, db: Session, *, row: models.IssueLifecycle) -> None:
        """Preserve authorization/execution history before an explicit replacement."""
        archived = self.put(
            db,
            account_id=row.account_id,
            issue_id=row.issue_id,
            kind="pickup",
            revision=row.data.get("authorization_id", row.data["issue_revision"]),
            state="superseded",
            data=dict(row.data),
        )
        archived.execution_id = row.execution_id
        row.execution_id = None
        db.flush()

    def pickup_execution(
        self, db: Session, *, row: models.IssueLifecycle
    ) -> models.FlowExecution | None:
        """Read the bound execution inside the issue authorization transaction."""
        return (
            db.get(models.FlowExecution, row.execution_id) if row.execution_id else None
        )

    def create_execution(
        self,
        db: Session,
        *,
        row: models.IssueLifecycle,
        flow_id: UUID,
        event: dict[str, Any],
        retry_of_execution_id: UUID | None = None,
    ) -> models.FlowExecution:
        """Atomically attach one fresh conversation to an audit operation."""
        if row.execution_id:
            execution = db.get(models.FlowExecution, row.execution_id)
            if execution is None:
                raise ValueError("lifecycle_execution_missing")
            return execution
        execution = models.FlowExecution(
            flow_id=flow_id,
            status="PENDING",
            trigger_event_details=event,
            retry_of_execution_id=retry_of_execution_id,
        )
        db.add(execution)
        db.flush()
        row.execution_id = execution.id
        db.flush()
        return execution


crud_issue_lifecycle = CRUDIssueLifecycle()
