"""Anthropic usage import connection and actor mappings (#1413).

One row per account holds the Anthropic Admin API key (as a
``SecretReference``) that a daily job uses to read the Claude Code Analytics
API. The imported numbers land in ``provider_billing_snapshot`` with
``provider='anthropic_cc'`` and ``usage_source='imported'``. They are daily
aggregates: they create no sessions and no ``api_usage`` rows and never feed
budgets or ingestion quota.

A separate table rather than a generalised ``copilot_import_connection``:
the Copilot row is shaped around a GitHub organization (seat price, per-user
billing status, enterprise token), so sharing it would mean a ``provider``
column, a new unique key and nullable Copilot-only columns.

``anthropic_user_mapping`` ties one imported actor (a canonical email, or
``key:<api key name>``) to one Preloop user of the same account. An explicit
mapping wins over the automatic match by member email.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class AnthropicImportConnection(Base):
    """Account-scoped link to one Anthropic organization's usage reports."""

    __tablename__ = "anthropic_import_connection"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Admin API key, stored encrypted behind a secret reference.
    secret_reference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("secret_reference.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Last four characters of the Admin key, shown so an admin can tell keys
    # apart. Never more than four characters.
    key_hint: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    # Operator-listed Anthropic API key names that Preloop uses as upstream
    # credentials. Usage under these names is already metered by the gateway.
    gateway_key_names: Mapped[Optional[List[str]]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Most recent report day fully imported; the next run resumes after it.
    last_synced_day: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_warning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account = relationship("Account")
    secret_reference = relationship("SecretReference")

    __table_args__ = (
        UniqueConstraint("account_id", name="uq_anthropic_import_connection_account"),
    )


class AnthropicUserMapping(Base):
    """One imported Anthropic actor mapped to one Preloop user.

    ``actor`` is the canonical form: a lowercased email for OAuth users, or
    ``key:<api key name>`` for API key actors.
    """

    __tablename__ = "anthropic_user_mapping"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("anthropic_import_connection.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )

    account = relationship("Account")
    connection = relationship("AnthropicImportConnection")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("account_id", "actor", name="uq_anthropic_user_mapping_actor"),
        Index("ix_anthropic_user_mapping_account_user", "account_id", "user_id"),
    )
