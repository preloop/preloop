"""Persistence for the per-tracker-issue cost rollup (#958).

All reads are scoped to one account. Writes are upserts keyed on the natural
identity of each row (issue, execution, pull request), so recording the same
execution or webhook twice converges on the same state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from preloop.models import models

#: Terminal statuses that count as a failed run. STOPPED and CANCELLED are
#: operator decisions, not failures, so they count as runs but not here.
FAILED_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {"FAILED", "TIMEOUT", "TIMED_OUT", "ABORTED"}
)


@dataclass(frozen=True)
class FlowFactTotals:
    """Sums of the execution facts of one issue, optionally for one flow."""

    rollup_id: uuid.UUID
    total_tokens: int
    estimated_cost: Decimal
    run_count: int
    failed_run_count: int


class CRUDIssueCost:
    """Rollup rows, execution facts and pull request timestamps."""

    # --- identity lookups -------------------------------------------------

    def lifecycle_issue_for_execution(
        self, db: Session, *, account_id: uuid.UUID, execution_id: uuid.UUID
    ) -> Optional[models.Issue]:
        """Issue a lifecycle operation (triage, pickup, audit) bound to a run.

        Args:
            db: Database session.
            account_id: Owning account.
            execution_id: The execution the lifecycle row points at.

        Returns:
            The bound issue, or None when no lifecycle row names the run.
        """
        return db.scalar(
            select(models.Issue)
            .join(
                models.IssueLifecycle,
                models.IssueLifecycle.issue_id == models.Issue.id,
            )
            .where(
                models.IssueLifecycle.account_id == account_id,
                models.IssueLifecycle.execution_id == execution_id,
            )
            .limit(1)
        )

    def find_issues_by_key(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        issue_key: str,
        tracker_id: Optional[uuid.UUID] = None,
        limit: int = 2,
    ) -> list[models.Issue]:
        """Synced issues whose key matches, case-insensitively.

        Args:
            db: Database session.
            account_id: Owning account.
            issue_key: Canonical tracker key (``org/repo#12`` or ``PROJ-7``).
            tracker_id: Restrict to one tracker when known.
            limit: Row cap; callers ask for two to detect ambiguity.

        Returns:
            Up to ``limit`` matching issues.
        """
        query = (
            select(models.Issue)
            .join(models.Tracker, models.Issue.tracker_id == models.Tracker.id)
            .where(
                models.Tracker.account_id == account_id,
                func.lower(models.Issue.key) == issue_key.lower(),
            )
            .limit(limit)
        )
        if tracker_id is not None:
            query = query.where(models.Issue.tracker_id == tracker_id)
        return list(db.scalars(query))

    def tracker_belongs_to_account(
        self, db: Session, *, account_id: uuid.UUID, tracker_id: uuid.UUID
    ) -> bool:
        """Whether a tracker id named in a trigger payload is the account's own."""
        return (
            db.scalar(
                select(
                    exists().where(
                        models.Tracker.id == tracker_id,
                        models.Tracker.account_id == account_id,
                    )
                )
            )
            is True
        )

    def project_belongs_to_account(
        self, db: Session, *, account_id: uuid.UUID, project_id: uuid.UUID
    ) -> bool:
        """Whether a project id named in a trigger payload is the account's own."""
        return (
            db.scalar(
                select(
                    exists()
                    .where(models.Project.id == project_id)
                    .where(
                        models.Project.organization_id == models.Organization.id,
                        models.Organization.tracker_id == models.Tracker.id,
                        models.Tracker.account_id == account_id,
                    )
                )
            )
            is True
        )

    def get_execution(
        self, db: Session, *, account_id: uuid.UUID, execution_id: Any
    ) -> Optional[models.FlowExecution]:
        """Load one execution of the account."""
        try:
            execution_uuid = uuid.UUID(str(execution_id))
        except (TypeError, ValueError):
            return None
        return db.scalar(
            select(models.FlowExecution)
            .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
            .where(
                models.FlowExecution.id == execution_uuid,
                models.Flow.account_id == account_id,
            )
        )

    def get_flow(self, db: Session, *, flow_id: uuid.UUID) -> Optional[models.Flow]:
        """Load the flow an execution belongs to."""
        return db.get(models.Flow, flow_id)

    # --- rollup rows -------------------------------------------------------

    def get_rollup(
        self, db: Session, *, account_id: uuid.UUID, rollup_id: uuid.UUID
    ) -> Optional[models.IssueCostRollup]:
        """Load one rollup row of the account."""
        return db.scalar(
            select(models.IssueCostRollup).where(
                models.IssueCostRollup.id == rollup_id,
                models.IssueCostRollup.account_id == account_id,
            )
        )

    def get_or_create_rollup(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        tracker_id: uuid.UUID,
        issue_key: str,
    ) -> models.IssueCostRollup:
        """Return the issue's rollup row, inserting it race-free when missing.

        Args:
            db: Database session.
            account_id: Owning account.
            tracker_id: Tracker the issue lives in.
            issue_key: Canonical issue key.

        Returns:
            The persisted row.
        """
        db.execute(
            pg_insert(models.IssueCostRollup)
            .values(
                id=uuid.uuid4(),
                account_id=account_id,
                tracker_id=tracker_id,
                issue_key=issue_key,
            )
            .on_conflict_do_nothing(constraint="uq_issue_cost_rollup_issue")
        )
        row = db.scalar(
            select(models.IssueCostRollup).where(
                models.IssueCostRollup.account_id == account_id,
                models.IssueCostRollup.tracker_id == tracker_id,
                models.IssueCostRollup.issue_key == issue_key,
            )
        )
        assert row is not None  # inserted above or by a concurrent writer
        return row

    def find_rollup(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        tracker_id: uuid.UUID,
        issue_key: str,
    ) -> Optional[models.IssueCostRollup]:
        """The issue's rollup row, without creating one."""
        return db.scalar(
            select(models.IssueCostRollup).where(
                models.IssueCostRollup.account_id == account_id,
                models.IssueCostRollup.tracker_id == tracker_id,
                models.IssueCostRollup.issue_key == issue_key,
            )
        )

    def describe_rollup(
        self,
        db: Session,
        *,
        rollup: models.IssueCostRollup,
        issue_id: Optional[uuid.UUID],
        project_id: Optional[uuid.UUID],
        title: Optional[str],
        issue_url: Optional[str],
    ) -> None:
        """Fill descriptive columns that are still empty; never overwrite."""
        if rollup.issue_id is None and issue_id is not None:
            rollup.issue_id = issue_id
        if rollup.project_id is None and project_id is not None:
            rollup.project_id = project_id
        if not rollup.title and title:
            rollup.title = title[:512]
        if not rollup.issue_url and issue_url:
            rollup.issue_url = issue_url[:1000]
        db.flush()

    def recompute_rollup(
        self, db: Session, *, rollup_id: uuid.UUID
    ) -> Optional[models.IssueCostRollup]:
        """Recompute every maintained column from facts and pull requests.

        The row is locked first so two executions finishing together for one
        issue serialize: the second recompute starts after the first commits
        and so reads both facts.

        Args:
            db: Database session.
            rollup_id: Row to recompute.

        Returns:
            The recomputed row, or None when it no longer exists.
        """
        rollup = db.scalar(
            select(models.IssueCostRollup)
            .where(models.IssueCostRollup.id == rollup_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if rollup is None:
            return None
        fact = models.IssueCostExecution
        sums = db.execute(
            select(
                func.coalesce(func.sum(fact.total_tokens), 0),
                func.coalesce(func.sum(fact.estimated_cost), 0),
                func.count(fact.id),
                func.count(fact.id).filter(
                    fact.status.in_(tuple(FAILED_EXECUTION_STATUSES))
                ),
                func.min(fact.start_time),
            ).where(fact.rollup_id == rollup_id)
        ).one()
        rollup.total_tokens = int(sums[0])
        rollup.estimated_cost = Decimal(sums[1])
        rollup.run_count = int(sums[2])
        rollup.failed_run_count = int(sums[3])
        rollup.first_event_at = sums[4]

        pull = models.IssueCostPullRequest
        pulls = list(
            db.scalars(
                select(pull)
                .where(pull.rollup_id == rollup_id, pull.ambiguous.is_(False))
                .order_by(pull.opened_at.asc().nulls_last(), pull.created_at.asc())
            )
        )
        rollup.pr_url = pulls[0].pr_key if pulls else None
        rollup.pr_opened_at = _earliest(p.opened_at for p in pulls)
        rollup.approved_at = _earliest(p.approved_at for p in pulls)
        rollup.merged_at = _earliest(p.merged_at for p in pulls)
        db.flush()
        return rollup

    # --- execution facts ---------------------------------------------------

    def get_fact(
        self, db: Session, *, execution_id: uuid.UUID
    ) -> Optional[models.IssueCostExecution]:
        """The fact recorded for one execution, if any."""
        return db.scalar(
            select(models.IssueCostExecution).where(
                models.IssueCostExecution.execution_id == execution_id
            )
        )

    def upsert_fact(
        self, db: Session, *, values: dict[str, Any]
    ) -> models.IssueCostExecution:
        """Insert or overwrite the fact of one execution (idempotent).

        Args:
            db: Database session.
            values: Column values; ``execution_id`` is the conflict key.

        Returns:
            The persisted fact.
        """
        statement = pg_insert(models.IssueCostExecution).values(
            id=uuid.uuid4(), **values
        )
        updatable = {key: statement.excluded[key] for key in values if key != "id"}
        updatable["updated_at"] = func.now()
        db.execute(
            statement.on_conflict_do_update(
                index_elements=[models.IssueCostExecution.execution_id],
                set_=updatable,
            )
        )
        fact = db.scalar(
            select(models.IssueCostExecution)
            .where(models.IssueCostExecution.execution_id == values["execution_id"])
            .execution_options(populate_existing=True)
        )
        assert fact is not None
        return fact

    def unassign_pull_request_facts(
        self, db: Session, *, account_id: uuid.UUID, pr_key: str
    ) -> set[uuid.UUID]:
        """Detach facts attributed only through a now-ambiguous pull request.

        Args:
            db: Database session.
            account_id: Owning account.
            pr_key: The pull request that turned out to be shared.

        Returns:
            Rollup ids that lost a fact and need a recompute.
        """
        facts = list(
            db.scalars(
                select(models.IssueCostExecution).where(
                    models.IssueCostExecution.account_id == account_id,
                    models.IssueCostExecution.pr_key == pr_key,
                    models.IssueCostExecution.link.in_(
                        ("pull_request", "closing_reference")
                    ),
                    models.IssueCostExecution.rollup_id.is_not(None),
                )
            )
        )
        touched: set[uuid.UUID] = set()
        for fact in facts:
            if fact.rollup_id is not None:
                touched.add(fact.rollup_id)
            fact.rollup_id = None
            fact.link = "ambiguous"
        db.flush()
        return touched

    def update_fact_cost(
        self, db: Session, *, execution_id: uuid.UUID, estimated_cost: Any
    ) -> Optional[uuid.UUID]:
        """Copy a repriced execution cost onto its fact.

        Returns:
            The rollup id to recompute, or None when there is no attributed fact.
        """
        fact = self.get_fact(db, execution_id=execution_id)
        if fact is None:
            return None
        fact.estimated_cost = estimated_cost
        db.flush()
        return fact.rollup_id

    # --- pull requests -----------------------------------------------------

    def get_pull_request(
        self, db: Session, *, account_id: uuid.UUID, pr_key: str
    ) -> Optional[models.IssueCostPullRequest]:
        """One pull request row of the account."""
        return db.scalar(
            select(models.IssueCostPullRequest)
            .where(
                models.IssueCostPullRequest.account_id == account_id,
                models.IssueCostPullRequest.pr_key == pr_key,
            )
            .execution_options(populate_existing=True)
        )

    def get_or_create_pull_request(
        self, db: Session, *, account_id: uuid.UUID, pr_key: str
    ) -> models.IssueCostPullRequest:
        """Return the pull request row, inserting it race-free when missing."""
        db.execute(
            pg_insert(models.IssueCostPullRequest)
            .values(id=uuid.uuid4(), account_id=account_id, pr_key=pr_key)
            .on_conflict_do_nothing(constraint="uq_issue_cost_pull_request")
        )
        row = db.scalar(
            select(models.IssueCostPullRequest)
            .where(
                models.IssueCostPullRequest.account_id == account_id,
                models.IssueCostPullRequest.pr_key == pr_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert row is not None
        return row

    # --- reads for the report ----------------------------------------------

    def list_rollups(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        start: Optional[datetime],
        end: Optional[datetime],
        project_id: Optional[uuid.UUID],
        flow_id: Optional[uuid.UUID],
        limit: int,
    ) -> list[models.IssueCostRollup]:
        """Issue rows whose first event falls in the period.

        Args:
            db: Database session.
            account_id: Owning account.
            start: Inclusive lower bound on ``first_event_at``.
            end: Exclusive upper bound on ``first_event_at``.
            project_id: Only issues of this project.
            flow_id: Only issues this flow contributed to.
            limit: Row cap.

        Returns:
            Matching rows, most expensive first.
        """
        rollup = models.IssueCostRollup
        query = select(rollup).where(
            rollup.account_id == account_id, rollup.run_count > 0
        )
        if start is not None:
            query = query.where(rollup.first_event_at >= start)
        if end is not None:
            query = query.where(rollup.first_event_at < end)
        if project_id is not None:
            query = query.where(rollup.project_id == project_id)
        if flow_id is not None:
            query = query.where(
                exists().where(
                    models.IssueCostExecution.rollup_id == rollup.id,
                    models.IssueCostExecution.flow_id == flow_id,
                )
            )
        query = query.order_by(
            rollup.estimated_cost.desc(), rollup.first_event_at.desc(), rollup.id
        ).limit(limit)
        return list(db.scalars(query))

    def fact_totals_by_rollup_and_flow(
        self, db: Session, *, rollup_ids: Sequence[uuid.UUID]
    ) -> list[tuple[uuid.UUID, uuid.UUID, int, Decimal, int, int]]:
        """Per (issue, flow) sums over the facts of the given issues.

        Returns:
            ``(rollup_id, flow_id, tokens, cost, runs, failed_runs)`` tuples.
        """
        if not rollup_ids:
            return []
        fact = models.IssueCostExecution
        rows = db.execute(
            select(
                fact.rollup_id,
                fact.flow_id,
                func.coalesce(func.sum(fact.total_tokens), 0),
                func.coalesce(func.sum(fact.estimated_cost), 0),
                func.count(fact.id),
                func.count(fact.id).filter(
                    fact.status.in_(tuple(FAILED_EXECUTION_STATUSES))
                ),
            )
            .where(fact.rollup_id.in_(list(rollup_ids)))
            .group_by(fact.rollup_id, fact.flow_id)
        ).all()
        return [
            (row[0], row[1], int(row[2]), Decimal(row[3]), int(row[4]), int(row[5]))
            for row in rows
        ]

    def unassigned_totals(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        project_id: Optional[uuid.UUID] = None,
        flow_id: Optional[uuid.UUID] = None,
    ) -> tuple[int, Decimal, int, int]:
        """Sums over the facts attributed to no issue, uncapped.

        Uses the same filters as ``list_facts(unassigned=True)``.

        Args:
            db: Database session.
            account_id: Owning account.
            start: Inclusive lower bound on execution start.
            end: Exclusive upper bound on execution start.
            project_id: Only facts of this project.
            flow_id: Only facts of this flow.

        Returns:
            ``(tokens, cost, runs, failed_runs)``.
        """
        fact = models.IssueCostExecution
        query = (
            select(
                func.coalesce(func.sum(fact.total_tokens), 0),
                func.coalesce(func.sum(fact.estimated_cost), 0),
                func.count(fact.id),
                func.count(fact.id).filter(
                    fact.status.in_(tuple(FAILED_EXECUTION_STATUSES))
                ),
            )
            .join(models.Flow, models.Flow.id == fact.flow_id)
            .where(fact.account_id == account_id, fact.rollup_id.is_(None))
        )
        if start is not None:
            query = query.where(fact.start_time >= start)
        if end is not None:
            query = query.where(fact.start_time < end)
        if project_id is not None:
            query = query.where(fact.project_id == project_id)
        if flow_id is not None:
            query = query.where(fact.flow_id == flow_id)
        row = db.execute(query).one()
        return int(row[0]), Decimal(row[1]), int(row[2]), int(row[3])

    def list_facts(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        rollup_ids: Optional[Sequence[uuid.UUID]] = None,
        unassigned: bool = False,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        project_id: Optional[uuid.UUID] = None,
        flow_id: Optional[uuid.UUID] = None,
        limit: int = 5000,
    ) -> list[tuple[models.IssueCostExecution, str]]:
        """Facts with their flow name, for issue drill-down and unassigned.

        Args:
            db: Database session.
            account_id: Owning account.
            rollup_ids: Facts of these issues.
            unassigned: Facts attributed to no issue instead.
            start: Inclusive lower bound on execution start (unassigned only).
            end: Exclusive upper bound on execution start (unassigned only).
            project_id: Only facts of this project (unassigned only).
            flow_id: Only facts of this flow.
            limit: Row cap.

        Returns:
            ``(fact, flow_name)`` pairs, oldest first.
        """
        fact = models.IssueCostExecution
        query = (
            select(fact, models.Flow.name)
            .join(models.Flow, models.Flow.id == fact.flow_id)
            .where(fact.account_id == account_id)
        )
        if unassigned:
            query = query.where(fact.rollup_id.is_(None))
            if start is not None:
                query = query.where(fact.start_time >= start)
            if end is not None:
                query = query.where(fact.start_time < end)
            if project_id is not None:
                query = query.where(fact.project_id == project_id)
        else:
            if not rollup_ids:
                return []
            query = query.where(fact.rollup_id.in_(list(rollup_ids)))
        if flow_id is not None:
            query = query.where(fact.flow_id == flow_id)
        query = query.order_by(fact.start_time.asc(), fact.id).limit(limit)
        return [(row[0], str(row[1] or "")) for row in db.execute(query).all()]

    def names(
        self,
        db: Session,
        *,
        tracker_ids: Iterable[uuid.UUID],
        project_ids: Iterable[uuid.UUID],
        flow_ids: Iterable[uuid.UUID],
    ) -> tuple[
        dict[uuid.UUID, tuple[str, str]], dict[uuid.UUID, str], dict[uuid.UUID, str]
    ]:
        """Display names for trackers (name, type), projects and flows."""
        tracker_list = [t for t in set(tracker_ids) if t]
        project_list = [p for p in set(project_ids) if p]
        flow_list = [f for f in set(flow_ids) if f]
        trackers = (
            {
                row[0]: (str(row[1] or ""), str(row[2] or ""))
                for row in db.execute(
                    select(
                        models.Tracker.id,
                        models.Tracker.name,
                        models.Tracker.tracker_type,
                    ).where(models.Tracker.id.in_(tracker_list))
                ).all()
            }
            if tracker_list
            else {}
        )
        projects = (
            {
                row[0]: str(row[1] or "")
                for row in db.execute(
                    select(models.Project.id, models.Project.name).where(
                        models.Project.id.in_(project_list)
                    )
                ).all()
            }
            if project_list
            else {}
        )
        flows = (
            {
                row[0]: str(row[1] or "")
                for row in db.execute(
                    select(models.Flow.id, models.Flow.name).where(
                        models.Flow.id.in_(flow_list)
                    )
                ).all()
            }
            if flow_list
            else {}
        )
        return trackers, projects, flows

    def terminal_executions_without_fact(
        self,
        db: Session,
        *,
        account_id: uuid.UUID,
        start: datetime,
        end: datetime,
        terminal_statuses: Iterable[str],
        limit: int,
    ) -> list[models.FlowExecution]:
        """Finished executions of the period that have no fact yet.

        Used by the rebuild endpoint to pick up history and runs that ended
        on a path that does not reach the orchestrator's terminal hook.
        """
        execution = models.FlowExecution
        return list(
            db.scalars(
                select(execution)
                .join(models.Flow, models.Flow.id == execution.flow_id)
                .where(
                    models.Flow.account_id == account_id,
                    execution.status.in_(tuple(terminal_statuses)),
                    execution.start_time >= start.replace(tzinfo=None),
                    execution.start_time < end.replace(tzinfo=None),
                    ~exists().where(
                        models.IssueCostExecution.execution_id == execution.id
                    ),
                )
                .order_by(execution.start_time.asc())
                .limit(limit)
            )
        )

    def commit(self, db: Session) -> None:
        """Commit the rollup write."""
        db.commit()

    def rollback(self, db: Session) -> None:
        """Discard a failed rollup write."""
        db.rollback()


def _earliest(values: Iterable[Optional[datetime]]) -> Optional[datetime]:
    present = [value for value in values if value is not None]
    return min(present) if present else None


crud_issue_cost = CRUDIssueCost()
