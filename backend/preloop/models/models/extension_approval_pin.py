"""Content hashes an approved extension is pinned to."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExtensionApprovalPin(Base):
    """One reviewed content hash under an account approval.

    An approval with no pins is a name approval; with pins, only the pinned
    hashes are approved and any other observed hash reads as changed.
    """

    __tablename__ = "extension_approval_pin"
    __table_args__ = (
        UniqueConstraint(
            "approval_id", "content_hash", name="uq_extension_approval_pin"
        ),
    )
    approval_id: Mapped[UUID] = mapped_column(
        ForeignKey("extension_approval.id", ondelete="CASCADE"), index=True
    )
    content_hash: Mapped[str] = mapped_column(String(80))
    version: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(512))
    pinned_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
