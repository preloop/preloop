"""Policy notice hits: model I/O rules with the ``notify`` action that matched."""

from datetime import datetime
from typing import Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from preloop.models.models.base import Base

#: Upper bound on a stored excerpt, in characters.
POLICY_NOTICE_EXCERPT_MAX_CHARS = 280


class PolicyNoticeHit(Base):
    """One ``notify`` match on one model call.

    A hit is written every time a notify rule matches, including repeats; the
    console and the weekly digest count them. The full prompt or completion is
    never stored: only its SHA-256 and a secret-scrubbed excerpt of at most
    :data:`POLICY_NOTICE_EXCERPT_MAX_CHARS` characters around the match.
    ``excerpt`` is null when redaction failed.

    ``notified_at`` is set on the one hit per rule, user and hour that sent an
    outbound message. It is the cross-replica debounce marker.
    """

    __tablename__ = "policy_notice_hit"
    __table_args__ = (
        Index(
            "ix_policy_notice_hit_account_rule_created",
            "account_id",
            "rule_id",
            "created_at",
        ),
        Index("ix_policy_notice_hit_account_created", "account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    target: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="model.request | model.response",
    )
    rule_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    excerpt: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Secret-scrubbed excerpt around the match, at most 280 chars",
    )
    notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set when this hit sent the outbound notice (debounce marker)",
    )

    def __repr__(self) -> str:
        return (
            f"<PolicyNoticeHit rule={self.rule_id} target={self.target} "
            f"account={str(self.account_id)[:8]}>"
        )
