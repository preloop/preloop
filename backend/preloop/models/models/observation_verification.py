"""Inert, source-scoped verification records for optional evidence processors."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ObservationVerification(Base):
    """A verifier result bound to one source, subject, policy and evidence digest.

    Collectors cannot create these records. Optional trusted verifiers own writes;
    absent records mean unverified. Retained records support expiry and audit.
    """

    __tablename__ = "observation_verification"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "source_ref",
            "subject_key",
            name="uq_observation_verification_scope",
        ),
        CheckConstraint(
            "expires_at > verified_at", name="ck_observation_verification_window"
        ),
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    source_ref: Mapped[UUID]
    subject_key: Mapped[str] = mapped_column(String(64))
    policy_revision: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(String(64))
