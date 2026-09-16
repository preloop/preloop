"""Per account progress of the session search corpus backfill.

One row per account, written by the backfill sweeper. It holds the watermark
(the oldest session the sweeper has already walked), so a restarted sweeper
resumes where it stopped instead of scanning an account's history again, and
it holds the completion stamp, so a finished account is skipped without
touching its sessions at all.

The watermark is also what the interface reads: "search reaches back to X" is
the watermark of an in progress backfill, and the whole retained history once
``completed_at`` is set.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account


class SessionSearchBackfillState(Base):
    """Backfill progress for one account."""

    __tablename__ = "session_search_backfill_state"
    __table_args__ = (
        Index(
            "ix_session_search_backfill_state_pending",
            "updated_at",
            postgresql_where=text("completed_at IS NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    #: Oldest session already walked: the next pass takes sessions strictly
    #: older than this, ordered by (started_at, id) descending. NULL means the
    #: account has never been walked, so the next pass starts at the newest
    #: session.
    cursor_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Tie break for the cursor: two sessions can share a start timestamp.
    cursor_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    #: Set once the walk reached the end of the account's retained history.
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_pass_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sessions_scanned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    rows_written: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Last failure for this account, kept so an account that keeps failing is
    #: visible without reading the logs. Cleared by the next clean pass.
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account: Mapped["Account"] = relationship("Account")

    def __repr__(self) -> str:
        return (
            f"<SessionSearchBackfillState(account_id={self.account_id}, "
            f"cursor_started_at={self.cursor_started_at}, "
            f"completed_at={self.completed_at})>"
        )
