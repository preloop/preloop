"""Durable owner grant: allow one flow on one runner until revoked."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RunnerFlowGrant(Base):
    """A runner owner's standing consent for one flow on one runner.

    Checked before a new ``owner_consent`` request is created for a
    ``runner_flow`` action. Revoking sets ``revoked_at``; rows are kept so the
    grant history stays auditable.
    """

    __tablename__ = "runner_flow_grant"
    __table_args__ = (
        Index(
            "uq_runner_flow_grant_active",
            "runner_id",
            "flow_id",
            "owner_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_runner.id", ondelete="CASCADE"),
        index=True,
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flow.id", ondelete="CASCADE"), index=True
    )
    approval_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approval_request.id", ondelete="SET NULL"),
        nullable=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
