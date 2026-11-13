"""Claim and record what the control plane posted for a review execution."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.platform_review_publication import (
    STATE_FAILED,
    STATE_POSTED,
    STATE_PUBLISHING,
    STATE_SKIPPED,
)

from .review_publication_receipt import normalize_repository

#: A claim older than this is treated as abandoned by a crashed worker.
CLAIM_STALE_AFTER = timedelta(minutes=10)

_TERMINAL_STATUSES = (
    "SUCCEEDED",
    "FAILED",
    "STOPPED",
    "TIMEOUT",
    "TIMED_OUT",
    "ABORTED",
    "CANCELLED",
    "CANCELED",
)


def _now() -> datetime:
    """Naive UTC, the convention of the timestamp columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CRUDPlatformReviewPublication:
    """The only writer of ``platform_review_publication``."""

    def get_for_execution(
        self, db: Session, *, execution_id: UUID | str
    ) -> Optional[models.PlatformReviewPublication]:
        """Read the current row of one execution, bypassing cached state."""
        return (
            db.query(models.PlatformReviewPublication)
            .populate_existing()
            .filter(
                models.PlatformReviewPublication.execution_id == UUID(str(execution_id))
            )
            .first()
        )

    def claim(
        self,
        db: Session,
        *,
        account_id: UUID | str,
        execution_id: UUID | str,
        repository: str,
        pull_request: str | int,
        head_sha: str,
        reclaim_states: Sequence[str] = (STATE_FAILED,),
        retryable_only: bool = True,
    ) -> Optional[models.PlatformReviewPublication]:
        """Take the exclusive right to publish for one execution.

        The first caller inserts the row. A later caller takes it over only
        when the row is in one of ``reclaim_states`` or its claim is stale.
        Both paths are single statements, so two workers never both win.

        Args:
            db: Database session.
            account_id: Owning account of the execution.
            execution_id: Execution to publish for.
            repository: ``owner/repo`` of the execution's own binding.
            pull_request: Pull request number of that binding.
            head_sha: Reviewed commit of that binding.
            reclaim_states: Finished states this caller may retry.
            retryable_only: Retry a failed row only when it is marked
                retryable (automatic attempts); an explicit request retries
                any failed row.

        Returns:
            The claimed row, or None when another attempt holds or finished it.
        """
        model = models.PlatformReviewPublication
        execution = UUID(str(execution_id))
        now = _now()
        inserted = db.execute(
            insert(model)
            .values(
                id=model.generate_id(),
                account_id=UUID(str(account_id)),
                execution_id=execution,
                state=STATE_PUBLISHING,
                retryable=False,
                nonce=secrets.token_hex(16),
                attempts=1,
                claimed_at=now,
                repository=normalize_repository(repository),
                pull_request=str(pull_request),
                head_sha=str(head_sha).lower(),
            )
            .on_conflict_do_nothing(
                constraint="uq_platform_review_publication_execution"
            )
            .returning(model.id)
        ).first()
        if inserted is None:
            finished: Any = model.state.in_(list(reclaim_states))
            # A read-back mismatch is never cleared by another attempt: the
            # provider holds something that needs a person to look at it.
            finished = finished & or_(
                model.reason.is_(None), model.reason != "readback_mismatch"
            )
            if retryable_only:
                finished = finished & (
                    (model.state != STATE_FAILED) | model.retryable.is_(True)
                )
            updated = (
                db.query(model)
                .filter(
                    model.execution_id == execution,
                    # The target of a row never changes after its first claim.
                    model.repository == normalize_repository(repository),
                    model.pull_request == str(pull_request),
                    model.head_sha == str(head_sha).lower(),
                    or_(
                        finished,
                        (model.state == STATE_PUBLISHING)
                        & (model.claimed_at < now - CLAIM_STALE_AFTER),
                    ),
                )
                .update(
                    {
                        model.state: STATE_PUBLISHING,
                        model.claimed_at: now,
                        model.attempts: model.attempts + 1,
                        model.reason: None,
                        model.retryable: False,
                        model.next_attempt_at: None,
                    },
                    synchronize_session=False,
                )
            )
            if not updated:
                db.commit()
                return None
        db.commit()
        return self.get_for_execution(db, execution_id=execution)

    def record(
        self, db: Session, *, row: models.PlatformReviewPublication, **values: Any
    ) -> models.PlatformReviewPublication:
        """Persist and commit progress or the outcome of the held claim."""
        for name, value in values.items():
            setattr(row, name, value)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def due_execution_ids(
        self, db: Session, *, limit: int = 20, max_attempts: int = 6
    ) -> list[tuple[UUID, UUID]]:
        """Rows an automatic attempt should take, oldest first.

        Two kinds: failed, retryable rows whose backoff elapsed, and rows
        left ``publishing`` by an attempt that was cancelled or whose worker
        died (claim older than :data:`CLAIM_STALE_AFTER`). Abandoned rows
        stop being returned once they used ``max_attempts``.

        Returns:
            ``(account_id, execution_id)`` pairs.
        """
        model = models.PlatformReviewPublication
        now = _now()
        rows = (
            db.query(model.account_id, model.execution_id)
            .filter(
                or_(
                    (model.state == STATE_FAILED)
                    & model.retryable.is_(True)
                    & model.next_attempt_at.isnot(None)
                    & (model.next_attempt_at <= now),
                    (model.state == STATE_PUBLISHING)
                    & (model.claimed_at < now - CLAIM_STALE_AFTER)
                    & (model.attempts < max_attempts),
                )
            )
            .order_by(func.coalesce(model.next_attempt_at, model.claimed_at).asc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return [(row[0], row[1]) for row in rows]

    def unclaimed_execution_ids(
        self, db: Session, *, finished_within: timedelta, limit: int = 20
    ) -> list[tuple[UUID, UUID]]:
        """Finished executions of opted-in flows that no attempt has claimed.

        Covers a run that reached a terminal status outside the worker that
        would have published it (an operator stop, a crashed worker). Only
        executions that finished after the flow was last saved are returned,
        so turning the opt-in on never reaches back to runs that finished
        before it. A run this bound leaves out can still be published with
        the explicit publish call.

        Returns:
            ``(account_id, execution_id)`` pairs, oldest first.
        """
        model = models.PlatformReviewPublication
        execution = models.FlowExecution
        flow = models.Flow
        enabled: Any = flow.git_clone_config[
            ("platform_review", "enabled")
        ].as_boolean()
        rows = (
            db.query(flow.account_id, execution.id)
            .join(flow, flow.id == execution.flow_id)
            .outerjoin(model, model.execution_id == execution.id)
            .filter(
                model.id.is_(None),
                execution.ci_review_binding.isnot(None),
                execution.ci_principal_id.isnot(None),
                execution.status.in_(_TERMINAL_STATUSES),
                execution.end_time.isnot(None),
                execution.end_time >= _now() - finished_within,
                # Leave the finishing worker time to publish first.
                execution.end_time <= _now() - timedelta(minutes=2),
                enabled.is_(True),
                execution.end_time >= flow.updated_at,
            )
            .order_by(execution.end_time.asc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return [(row[0], row[1]) for row in rows]


crud_platform_review_publication = CRUDPlatformReviewPublication()

__all__ = [
    "CLAIM_STALE_AFTER",
    "STATE_FAILED",
    "STATE_POSTED",
    "STATE_PUBLISHING",
    "STATE_SKIPPED",
    "crud_platform_review_publication",
]
