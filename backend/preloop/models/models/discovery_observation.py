"""Immutable safe discovery evidence, separate from candidate deduplication."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DiscoveryObservation(Base):
    """One authenticated source's collection window; not attestation."""

    __tablename__ = "discovery_observation"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "workstation_fingerprint",
            "source_ref",
            "observation_id",
            name="uq_discovery_observation_source",
        ),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workstation_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    source_ref: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
