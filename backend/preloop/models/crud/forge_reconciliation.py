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
#: An unpublished delivery claim younger than this belongs to a request that
#: may still publish; an older one is taken over by the next delivery.
UNPUBLISHED_CLAIM_GRACE = timedelta(minutes=2)
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
        claimed_due: Optional[datetime] = None,
    ) -> tuple[Optional[models.IssueCostPullRequest], bool]:
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
            claimed_due: The due time ``claim_due`` stamped on the row. When
                the row now holds another one, a webhook marked it during the
                read; that mark is kept so the next tick reads it again.

        Returns:
            The updated row (or None) and whether this locked write first recorded a merge.
        """
        row = db.get(
            models.IssueCostPullRequest,
            pull_request_id,
            with_for_update=True,
            populate_existing=True,
        )
        if row is None:
            return None, False
        previous_merged_at = row.merged_at
        marked_mid_read = claimed_due is not None and row.refresh_due_at != claimed_due
        final = row.state == "merged" or previous_merged_at is not None
        if final:
            row.state = "merged"
        if not not_modified:
            # The read that observes the merge still records the merged head.
            if state is not None and not final:
                row.state = state
            if (
                head_sha
                and (not final or not row.head_sha)
                # A prefix of the stored hash is the same commit, less exact.
                and not (row.head_sha or "").startswith(head_sha)
            ):
                row.head_sha = head_sha
            if merged_at is not None and row.merged_at is None:
                row.merged_at = merged_at
        if etag or not not_modified:
            # A plain 200 can stop offering validators. Never keep sending a
            # validator for an older representation in that case.
            row.refresh_etag = etag[:255] if etag else None
        row.refreshed_at = now
        if not marked_mid_read:
            row.refresh_due_at = (
                None if row.state in {"merged", "closed"} else now + REFRESH_REARM
            )
        merged_now = row.merged_at is not None and previous_merged_at is None
        if merged_now and row.rollup_id:
            # Persist the derived cycle-time fact with the merge itself. A
            # terminal row will not be retried by the normal refresh cadence.
            from .issue_cost import crud_issue_cost

            db.flush()
            crud_issue_cost.recompute_rollup(db, rollup_id=row.rollup_id)
        db.commit()
        return row, merged_now

    def defer(
        self,
        db: Session,
        *,
        pull_request_ids: list[uuid.UUID],
        until: datetime,
        claimed_due: Optional[datetime] = None,
    ) -> None:
        """Push claimed rows to ``until``: a throttled or unreadable forge.

        With ``claimed_due``, a row a webhook marked since the claim keeps its
        mark.
        """
        if not pull_request_ids:
            return
        p = models.IssueCostPullRequest
        query = update(p).where(p.id.in_(pull_request_ids))
        if claimed_due is not None:
            query = query.where(p.refresh_due_at == claimed_due)
        db.execute(query.values(refresh_due_at=until))
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

        A claim that was never published and is older than
        ``UNPUBLISHED_CLAIM_GRACE`` is taken over: its holder died between
        the claim and the queue acknowledgement, so this delivery is queued.

        Returns:
            True when this delivery should be queued, False when the id is
            published already or claimed moments ago by a concurrent request.
        """
        d = models.ForgeWebhookDelivery
        statement = pg_insert(d).values(
            id=uuid.uuid4(),
            tracker_id=tracker_id,
            delivery_id=delivery_id[:255],
            received_at=now,
        )
        claimed = db.scalar(
            statement.on_conflict_do_update(
                constraint="uq_forge_webhook_delivery",
                set_={"received_at": now, "published_at": None},
                where=and_(
                    d.published_at.is_(None),
                    d.received_at < now - UNPUBLISHED_CLAIM_GRACE,
                ),
            ).returning(d.id)
        )
        db.commit()
        return claimed is not None

    def mark_published(
        self,
        db: Session,
        *,
        tracker_id: uuid.UUID,
        delivery_id: str,
        now: datetime,
        claimed_at: datetime,
    ) -> None:
        """Acknowledge only the holder identified by ``claimed_at``.

        A late acknowledgement after takeover cannot publish the newer claim.
        """
        d = models.ForgeWebhookDelivery
        db.execute(
            update(d)
            .where(
                d.tracker_id == tracker_id,
                d.delivery_id == delivery_id[:255],
                d.received_at == claimed_at,
            )
            .values(published_at=now)
        )
        db.commit()

    def release(
        self,
        db: Session,
        *,
        tracker_id: uuid.UUID,
        delivery_id: str,
        claimed_at: datetime,
    ) -> None:
        """Drop this holder's unpublished claim so a redelivery can retry.

        A late failure after takeover cannot erase the newer holder's claim.
        """
        d = models.ForgeWebhookDelivery
        db.execute(
            delete(d).where(
                d.tracker_id == tracker_id,
                d.delivery_id == delivery_id[:255],
                d.received_at == claimed_at,
                d.published_at.is_(None),
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
