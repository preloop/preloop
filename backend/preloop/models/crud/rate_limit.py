"""Atomic fixed-window counters for the shared request rate limiter (EE #66)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models.rate_limit import RateLimitBucket


class CRUDRateLimit:
    """Counter operations; every call is one statement and commits nothing."""

    def hit(
        self, db: Session, *, key: str, window_start: datetime, window_s: int
    ) -> int:
        """Count one request against ``key`` and return the window total.

        A row left over from an earlier window restarts at 1. The upsert is a
        single statement, so concurrent replicas never lose an increment.

        Args:
            db: Session; the caller commits.
            key: Limiter key (rule plus hashed identity).
            window_start: Start of the current fixed window (naive UTC).
            window_s: Window length in seconds, used for ``expires_at``.

        Returns:
            Requests counted in the current window, including this one.
        """
        expires_at = window_start + timedelta(seconds=window_s)
        stmt = insert(RateLimitBucket).values(
            key=key, window_start=window_start, count=1, expires_at=expires_at
        )
        same_window = RateLimitBucket.window_start == stmt.excluded.window_start
        upsert = stmt.on_conflict_do_update(
            index_elements=[RateLimitBucket.key],
            set_={
                "count": case(
                    (same_window, RateLimitBucket.count + 1),
                    else_=1,
                ),
                "window_start": stmt.excluded.window_start,
                "expires_at": stmt.excluded.expires_at,
            },
        ).returning(RateLimitBucket.count)
        return int(db.execute(upsert).scalar_one())

    def peek(self, db: Session, *, key: str, window_start: datetime) -> int:
        """Return the count for ``key`` in the given window without changing it."""
        count = db.execute(
            select(RateLimitBucket.count).where(
                RateLimitBucket.key == key,
                RateLimitBucket.window_start == window_start,
            )
        ).scalar_one_or_none()
        return int(count or 0)

    def purge_expired(self, db: Session, *, now: datetime) -> int:
        """Delete rows whose window has ended. Returns the number deleted."""
        result = db.execute(
            delete(RateLimitBucket).where(RateLimitBucket.expires_at < now)
        )
        return int(getattr(result, "rowcount", 0) or 0)


crud_rate_limit = CRUDRateLimit()
