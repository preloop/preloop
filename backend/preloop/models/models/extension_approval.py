"""Account-wide extension decisions, independent of discovery retention."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExtensionApproval(Base):
    """One name approval shared by every device in an account."""

    __tablename__ = "extension_approval"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "agent_kind",
            "extension_kind",
            "extension_id",
            name="uq_extension_approval",
        ),
        CheckConstraint("approval_state IN ('approved','unapproved','unknown')"),
        CheckConstraint(
            "extension_kind IN ('plugin','skill','hook','command','mcp_server')",
            name="extension_approval_extension_kind_check",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE")
    )
    agent_kind: Mapped[str] = mapped_column(String(64))
    extension_kind: Mapped[str] = mapped_column(String(16))
    extension_id: Mapped[str] = mapped_column(String(128))
    approval_state: Mapped[str] = mapped_column(String(16))
    version_constraint: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(512))
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
