"""Append-only history of account extension decisions.

``extension_approval`` holds the current decision and is overwritten in place.
Each change also appends one row here so the console can show who decided
what and when, including the state the decision replaced.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

DECISION_ACTIONS = ("state_changed", "pin_added", "pin_removed")


class ExtensionDecisionEvent(Base):
    """One approve, unapprove, reset, pin or unpin of an account extension."""

    __tablename__ = "extension_decision_event"
    __table_args__ = (
        CheckConstraint(
            "action IN ('state_changed','pin_added','pin_removed')",
            name="extension_decision_event_action_check",
        ),
        Index(
            "ix_extension_decision_event_account_created",
            "account_id",
            "created_at",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE")
    )
    agent_kind: Mapped[str] = mapped_column(String(64))
    extension_kind: Mapped[str] = mapped_column(String(16))
    extension_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))
    previous_state: Mapped[str | None] = mapped_column(String(16))
    new_state: Mapped[str | None] = mapped_column(String(16))
    content_hash: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(String(512))
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
