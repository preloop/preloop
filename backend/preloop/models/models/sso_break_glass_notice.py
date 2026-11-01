"""Durable email notifications for actual owner break-glass sign-ins."""

from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SsoBreakGlassNotice(Base):
    """One owner recipient per login; retries never create additional notices."""

    __tablename__ = "sso_break_glass_notice"
    __table_args__ = (UniqueConstraint("login_id", "recipient_email"),)

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    login_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
