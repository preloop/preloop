"""Provider billing ingestion models for cost reconciliation.

``ProviderBillingConnection`` stores one account's link to a provider's
billing/usage API (admin credential via SecretReference). Fetched actuals are
persisted as idempotent ``ProviderBillingSnapshot`` rows so estimated spend in
``ApiUsage`` can be reconciled against what the provider actually billed.
The tables live in the OSS models package (shared alembic tree); the fetchers
and endpoints live in the Enterprise billing plugin.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class ProviderBillingConnection(Base):
    """Account-scoped connection to one provider's billing/usage API."""

    __tablename__ = "provider_billing_connection"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_reference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account = relationship("Account")
    secret_reference = relationship("SecretReference")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "provider", name="uq_provider_billing_connection"
        ),
    )


class ProviderBillingSnapshot(Base):
    """One provider-reported billing/usage bucket (idempotent upsert)."""

    __tablename__ = "provider_billing_snapshot"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    granularity: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    bucket_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Grouping dimensions as reported by the provider (nullable — depends on
    # the group_by supported per endpoint).
    model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    line_item: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_api_key_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    project_or_workspace_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    service_tier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Per-user dimension (a provider-side login such as a GitHub handle).
    # NULL for organization aggregates and for providers without users.
    user_login: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # ``imported`` marks rows pulled from a provider that the gateway never
    # metered (for example GitHub Copilot). NULL for reconciliation actuals.
    usage_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # ``reconciled`` for provider-billed amounts; NULL when the row carries
    # no dollar amount (seat or adoption rows).
    cost_basis: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    cost_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    uncached_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    cached_input_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    cache_creation_tokens: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    output_tokens: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    raw: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Deterministic hash of (provider, granularity, bucket_start, model,
    # line_item, provider_api_key_id, project_or_workspace_id, service_tier,
    # and user_login when set) making re-fetches idempotent.
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)

    account = relationship("Account")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "dedup_key", name="uq_provider_billing_snapshot_dedup"
        ),
        Index(
            "ix_provider_billing_snapshot_window",
            "account_id",
            "provider",
            "bucket_start",
        ),
        CheckConstraint(
            "usage_source IS NULL OR usage_source IN ('imported')",
            name="ck_provider_billing_snapshot_usage_source",
        ),
        CheckConstraint(
            "cost_basis IS NULL OR cost_basis IN ('estimated', 'reconciled')",
            name="ck_provider_billing_snapshot_cost_basis",
        ),
    )
