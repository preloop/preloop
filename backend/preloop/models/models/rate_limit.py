"""Shared fixed-window counters for the request rate limiter (EE #66).

One row per (rule, hashed identity) key. The limiter increments ``count``
atomically with an upsert, so every API replica draws from the same budget.
Rows are disposable: a lost row only resets one caller's window early.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RateLimitBucket(Base):
    """Request count for one limiter key in its current fixed window.

    Attributes:
        key: ``<rule>:<sha256 of the identity>``; never a raw IP or username.
        window_start: Start of the window ``count`` belongs to (naive UTC).
        count: Requests seen in that window.
        expires_at: When the row may be purged (window end).
    """

    __tablename__ = "rate_limit_bucket"
    __table_args__ = (UniqueConstraint("key", name="uq_rate_limit_bucket_key"),)

    key: Mapped[str] = mapped_column(String(128), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
