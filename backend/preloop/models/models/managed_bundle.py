"""Registered policy revisions and durable, independently owned expectations."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RegisteredBundleRevision(Base):
    """Immutable manifest history; one active revision per account/app/platform."""

    __tablename__ = "registered_bundle_revision"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "app_id",
            "platform",
            "policy_revision_hash",
            name="uq_bundle_revision",
        ),
        UniqueConstraint("id", "account_id", name="uq_bundle_revision_account"),
        Index(
            "uq_bundle_active_scope",
            "account_id",
            "app_id",
            "platform",
            unique=True,
            postgresql_where=text("retired_at IS NULL"),
        ),
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    app_id: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(16))
    policy_revision_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ControlExpectation(Base):
    """Origin namespaces preserve manual rows during bundle regeneration."""

    __tablename__ = "control_expectation"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "origin",
            "source_ref",
            "workstation_fingerprint",
            "agent_kind",
            "config_path_hash",
            "axis",
            "policy_revision_hash",
            name="uq_control_expectation_tuple",
        ),
        ForeignKeyConstraint(
            ["bundle_revision_id", "account_id"],
            ["registered_bundle_revision.id", "registered_bundle_revision.account_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(origin = 'bundle' AND bundle_revision_id IS NOT NULL) OR (origin = 'manual' AND bundle_revision_id IS NULL)",
            name="ck_control_expectation_origin",
        ),
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[str] = mapped_column(String(16))
    bundle_revision_id: Mapped[UUID | None] = mapped_column(index=True)
    source_ref: Mapped[UUID] = mapped_column()
    workstation_fingerprint: Mapped[str] = mapped_column(String(64))
    agent_kind: Mapped[str] = mapped_column(String(64))
    config_path_hash: Mapped[str] = mapped_column(String(64))
    axis: Mapped[str] = mapped_column(String(32))
    policy_revision_hash: Mapped[str] = mapped_column(String(64))
    required_scope: Mapped[str] = mapped_column(String(32))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
