"""Approval event model for tracking detailed approval workflow events."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class ApprovalEvent(Base):
    """Individual events in an approval workflow for audit trail and agent polling.

    Tracks every step of the approval process: request creation, notifications sent,
    individual votes with comments, escalations, and final resolution.
    """

    __tablename__ = "approval_event"
    __table_args__ = (
        # One `viewed` row per authenticated viewer. Concurrent GETs race
        # past has_event(); the unique index makes the insert idempotent.
        Index(
            "uq_approval_event_viewed_actor",
            "approval_request_id",
            "event_type",
            "actor_id",
            unique=True,
            postgresql_where=text("event_type = 'viewed' AND actor_id IS NOT NULL"),
        ),
        # Anonymous token views have no actor_id; NULLs are distinct in a
        # unique index, so they need their own partial unique.
        Index(
            "uq_approval_event_viewed_anonymous",
            "approval_request_id",
            "event_type",
            unique=True,
            postgresql_where=text("event_type = 'viewed' AND actor_id IS NULL"),
        ),
    )

    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_request.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Reference to the approval request",
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="The account this event belongs to",
    )

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Event type: approval_requested, notification_sent, vote_received, escalation_triggered, approval_complete, tool_executed, expired, cancelled",
    )

    detail: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Human-readable description of the event",
    )

    comment: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Approver comment (for vote_received events)",
    )

    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="User who triggered the event (if applicable)",
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
        comment="When the event occurred",
    )

    # Relationships
    approval_request: Mapped["ApprovalRequest"] = relationship(
        "ApprovalRequest", back_populates="events"
    )

    def __repr__(self) -> str:
        return (
            f"<ApprovalEvent(type={self.event_type}, "
            f"request_id={self.approval_request_id}, "
            f"timestamp={self.timestamp})>"
        )
