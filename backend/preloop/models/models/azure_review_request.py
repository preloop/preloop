"""Durable explicit Azure pull request review requests."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AzureReviewRequest(Base):
    """At most one pending follow-up per flow and Azure PR."""

    __tablename__ = "azure_review_request"
    __table_args__ = (
        UniqueConstraint(
            "flow_id", "repository_id", "number", name="uq_azure_review_request"
        ),
    )
    flow_id: Mapped[UUID] = mapped_column(ForeignKey("flow.id", ondelete="CASCADE"))
    tracker_id: Mapped[UUID] = mapped_column(
        ForeignKey("tracker.id", ondelete="CASCADE")
    )
    repository_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    event: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
