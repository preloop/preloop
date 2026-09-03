"""Attention dismissal model: per-account mutes for console attention items."""

from datetime import datetime
from typing import Optional
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from preloop.models.models.base import Base


class AttentionDismissal(Base):
    """One muted console attention item per (account, item).

    The console derives "needs attention" on the client from data it already
    fetches, so a dismissal is stored by the item's stable id
    (``<kind>:<entity id>``) rather than by a foreign key to whatever the item
    was about.

    ``fingerprint`` is what makes a dismissal safe: it is the client's summary
    of *why* the item is showing (the id of the latest failed run, the sorted
    list of unpriced model aliases, the agent's onboarding and validation
    state). An item stays hidden only while its fingerprint is unchanged, so a
    new failure brings the item back instead of being silently muted forever.
    """

    __tablename__ = "attention_dismissal"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "item_id", name="uq_attention_dismissal_account_item"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Stable console item id: '<kind>:<entity id>'",
    )
    fingerprint: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Why the item was showing; a change un-hides the item",
    )
    reason: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="expected | snoozed | fixed",
    )
    snooze_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set for reason='snoozed'; the row is inert once passed",
    )
    dismissed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # ``created_at``/``updated_at`` come from Base (naive UTC, house
    # convention). ``snooze_until`` is tz-aware because it is compared against
    # "now" in application code, not only written and read back.

    def __repr__(self) -> str:
        return (
            f"<AttentionDismissal {self.item_id} reason={self.reason} "
            f"account={str(self.account_id)[:8]}>"
        )
