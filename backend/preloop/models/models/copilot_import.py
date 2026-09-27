"""GitHub Copilot usage import connection.

One row per account links Preloop to a GitHub organization so a daily job can
import Copilot seats, premium-request spend and usage-metrics reports. The
imported numbers land in ``provider_billing_snapshot`` with
``provider='copilot'`` and ``usage_source='imported'``. They are never gateway
usage: they do not count toward gateway totals, budgets or ingestion quota.

This connection is deliberately separate from ``ProviderBillingConnection``,
whose rows are consumed by the reconciliation ingest for providers the gateway
does meter.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class CopilotImportConnection(Base):
    """Account-scoped link to one GitHub organization's Copilot data."""

    __tablename__ = "copilot_import_connection"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # GitHub organization login (the ``{org}`` path segment).
    organization: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional enterprise slug for the enterprise premium-request route, used
    # when the organization route cannot return per-user rows.
    enterprise: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Organization token: seats, organization premium-request usage and the
    # usage-metrics reports.
    secret_reference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Optional enterprise billing reader token for the enterprise route. When
    # absent the organization token is tried there (an organization owner in
    # the enterprise is an allowed actor).
    enterprise_secret_reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Operator-entered monthly price per seat. NULL means "not entered": the
    # Cost page then shows seats without a dollar seat line.
    seat_price_monthly: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Most recent report day fully imported; the next run resumes after it.
    last_synced_day: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ``available`` or ``unavailable`` for per-user premium-request billing,
    # with the reason shown on the Cost page when unavailable.
    per_user_billing_status: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    per_user_billing_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Same pair for the usage-metrics reports (policy or permission gaps).
    metrics_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    metrics_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Non-fatal problem from the last successful sync (for example a seat
    # list GitHub truncated), shown beside the import status.
    last_warning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account = relationship("Account")
    secret_reference = relationship(
        "SecretReference", foreign_keys=[secret_reference_id]
    )
    enterprise_secret_reference = relationship(
        "SecretReference", foreign_keys=[enterprise_secret_reference_id]
    )

    __table_args__ = (
        UniqueConstraint("account_id", name="uq_copilot_import_connection_account"),
    )
