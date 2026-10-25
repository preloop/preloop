"""Bounded forge refresh of pull request records and webhook delivery dedupe.

The pull request record (``IssueCostPullRequest``) mirrors three forge facts:
``head_sha``, ``state`` and ``merged_at``. A scheduled tick refreshes the open
and not yet observed ones, a bounded batch at a time, each at most once per
re-arm interval; a webhook for a pull request makes it due at once.

``ForgeWebhookDelivery`` records each forge delivery id queued for processing
so a redelivery of the same event is acknowledged without queueing it again.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from preloop.models import models

#: How long a refreshed open pull request waits before it is due again.
REFRESH_REARM = timedelta(minutes=5)
#: A pull request first seen this long ago and never observed open is not
#: worth a forge read: four weekly reports look no further back.
REFRESH_LOOKBACK = timedelta(days=28)
#: Wait before retrying a pull request no configured tracker can read.
UNREADABLE_BACKOFF = timedelta(days=1)


class CRUDPullRequestRefresh:
    """Claim, record and accelerate pull request record refreshes."""

    def claim_due(
        self, db: Session, *, now: datetime, limit: int
    ) -> list[models.IssueCostPullRequest]:
        """Claim up to ``limit`` due pull requests and commit the claim.

        Due means ``refresh_due_at`` has passed (re-armed or webhook
        accelerated), or it is unset on an open pull request, or on one not
        yet observed that is recent and carries no merge time. Claiming moves
        ``refresh_due_at`` one re-arm interval ahead, so a worker that dies
        mid-read only delays the row; concurrent ticks skip locked rows.

        Args:
            db: Database session.
            now: Current time (timezone-aware).
            limit: The batch bound.

        Returns:
            The claimed rows, webhook-accelerated and longest-waiting first.
        """
        p = models.IssueCostPullRequest
        rows = list(
            db.scalars(
                select(p)
                .where(
                    or_(
                        p.refresh_due_at <= now,
                        and_(
                            p.refresh_due_at.is_(None),
                            or_(
                                p.state == "open",
                                and_(
                                    p.state.is_(None),
                                    p.merged_at.is_(None),
                                    p.created_at
                                    >= (now - REFRESH_LOOKBACK).replace(tzinfo=None),
                                ),
                            ),
                        ),
                    )
                )
                .order_by(p.refresh_due_at.asc().nulls_last(), p.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.refresh_due_at = now + REFRESH_REARM
        db.commit()
        return rows

    def record(
        self,
        db: Session,
        *,
        pull_request_id: uuid.UUID,
        now: datetime,
        etag: Optional[str],
        state: Optional[str] = None,
        head_sha: Optional[str] = None,
        merged_at: Optional[datetime] = None,
        not_modified: bool = False,
    ) -> Optional[models.IssueCostPullRequest]:
        """Record one forge read and re-arm the row.

        A merge is final: a later read that reports another state (a stale
        replica, an out-of-order cache) never moves a merged record back, and
        an existing ``merged_at`` is never replaced. A closed pull request can
        be reopened, so ``closed`` follows the forge. Open rows are due again
        after the re-arm interval; merged and closed rows are not scheduled
        until a webhook names them.

        Args:
            db: Database session.
            pull_request_id: The row.
            now: Read time.
            etag: Validator to send next time.
            state: Forge state, one of ``PULL_REQUEST_STATES``.
            head_sha: Current head commit.
            merged_at: Forge merge time.
            not_modified: The forge answered 304; only bookkeeping changes.

        Returns:
            The updated row, or None when it no longer exists.
        """
        row = db.get(models.IssueCostPullRequest, pull_request_id)
        if row is None:
            return None
        if not not_modified:
            # The read that observes the merge still records the merged head.
            final = row.state == "merged"
            if state is not None and not final:
                row.state = state
            if head_sha and (not final or not row.head_sha):
                row.head_sha = head_sha
            if merged_at is not None and row.merged_at is None:
                row.merged_at = merged_at
        if etag:
            row.refresh_etag = etag[:255]
        row.refreshed_at = now
        row.refresh_due_at = (
            None if row.state in {"merged", "closed"} else now + REFRESH_REARM
        )
        db.commit()
        return row

    def defer(
        self, db: Session, *, pull_request_ids: list[uuid.UUID], until: datetime
    ) -> None:
        """Push claimed rows to ``until``: a throttled or unreadable forge."""
        if not pull_request_ids:
            return
        p = models.IssueCostPullRequest
        db.execute(
            update(p).where(p.id.in_(pull_request_ids)).values(refresh_due_at=until)
        )
        db.commit()

    def request(
        self, db: Session, *, account_id: uuid.UUID, pr_key: str, now: datetime
    ) -> bool:
        """Make a known pull request due now; unknown ones are not created.

        A webhook only says something changed; the next tick reads what. This
        also reaches merged and closed rows, so a reopen is observed.

        Returns:
            Whether a row was marked due.
        """
        p = models.IssueCostPullRequest
        marked = db.scalars(
            update(p)
            .where(p.account_id == account_id, p.pr_key == pr_key)
            .values(refresh_due_at=now)
            .returning(p.id)
        ).all()
        db.commit()
        return bool(marked)


class CRUDForgeWebhookDelivery:
    """Durable claims on forge delivery ids."""

    def claim(
        self, db: Session, *, tracker_id: uuid.UUID, delivery_id: str, now: datetime
    ) -> bool:
        """Claim a delivery id for queueing and commit.

        Returns:
            True for the first claim, False when the id was already claimed:
            the delivery is a redelivery or a concurrent duplicate.
        """
        inserted = db.scalar(
            pg_insert(models.ForgeWebhookDelivery)
            .values(
                id=uuid.uuid4(),
                tracker_id=tracker_id,
                delivery_id=delivery_id[:255],
                received_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_forge_webhook_delivery")
            .returning(models.ForgeWebhookDelivery.id)
        )
        db.commit()
        return inserted is not None

    def release(self, db: Session, *, tracker_id: uuid.UUID, delivery_id: str) -> None:
        """Drop a claim whose task was never queued, so a redelivery is."""
        d = models.ForgeWebhookDelivery
        db.execute(
            delete(d).where(
                d.tracker_id == tracker_id, d.delivery_id == delivery_id[:255]
            )
        )
        db.commit()

    def prune(self, db: Session, *, before: datetime, limit: int) -> int:
        """Delete up to ``limit`` claims older than ``before``."""
        d = models.ForgeWebhookDelivery
        ids = list(
            db.scalars(
                select(d.id).where(d.received_at < before).order_by(d.id).limit(limit)
            )
        )
        if ids:
            db.execute(delete(d).where(d.id.in_(ids)))
        db.commit()
        return len(ids)


crud_pull_request_refresh = CRUDPullRequestRefresh()
crud_forge_webhook_delivery = CRUDForgeWebhookDelivery()
