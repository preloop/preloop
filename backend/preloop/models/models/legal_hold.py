"""Legal hold: the record of who froze what, why, and when it was released.

A hold is two things and they are deliberately kept apart.

The **row** in this table is the account-visible fact: an actor, a reason, a
timestamp, and a release with its own actor and reason. That is what a
regulator or a customer's counsel actually asks for, and it is why ``reason``
is NOT NULL: a hold nobody can explain is indistinguishable from a bug.

The **boolean** on ``flow_execution``, ``approval_request`` and
``flow_artifact`` is derived enforcement state, written in the same
transaction as the row. The purge and the evidence janitor are batch UPDATEs
and DELETEs over single tables; making them join a hold table on every pass
would be the wrong shape for the hot path and would make "did the purge miss a
held row?" a question about a join rather than about a column. Release
recomputes the boolean from whatever active holds remain, so two overlapping
holds cannot be lifted by releasing one.

A hold on an execution covers that execution's evidence packs too. Freezing a
run and leaving its evidence to expire in thirty days would be a hold in name
only.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base

#: A flow execution. Cascades to that execution's evidence artifacts.
HOLD_RESOURCE_EXECUTION = "execution"
#: One approval request.
HOLD_RESOURCE_APPROVAL = "approval"
#: One evidence pack (a ``flow_artifact`` row of kind ``evidence``).
HOLD_RESOURCE_EVIDENCE_PACK = "evidence_pack"

HOLD_RESOURCE_TYPES: tuple[str, ...] = (
    HOLD_RESOURCE_EXECUTION,
    HOLD_RESOURCE_APPROVAL,
    HOLD_RESOURCE_EVIDENCE_PACK,
)


class LegalHold(Base):
    """One hold placed on one resource, with its actor, reason and release."""

    __tablename__ = "legal_hold"
    __table_args__ = (
        # One *active* hold per resource. Two teams asking for the same freeze
        # is one freeze; the durable control is here rather than in a service
        # check that concurrent callers can race past.
        Index(
            "uq_legal_hold_active_resource",
            "account_id",
            "resource_type",
            "resource_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_legal_hold_account_released", "account_id", "released_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="execution | approval | evidence_pack",
    )
    resource_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Identifier of the held resource within this account",
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Why the records are frozen. Mandatory: an unexplained hold "
        "is indistinguishable from a bug.",
    )
    # ON DELETE SET NULL, like approval attribution: removing a user must not
    # erase the fact that a hold was placed.
    placed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    released_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    released_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    release_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Why the hold was lifted",
    )

    @property
    def active(self) -> bool:
        """True while the hold still blocks purge and payload expiry."""
        return self.released_at is None

    def __repr__(self) -> str:
        """Describe the hold without a lazy load."""
        state = "active" if self.released_at is None else "released"
        return (
            f"<LegalHold {self.resource_type}:{self.resource_id} {state} "
            f"placed_at={self.placed_at}>"
        )
