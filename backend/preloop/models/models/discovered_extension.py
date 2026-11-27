"""Installed extension metadata. Content for review lives in extension_content."""

from datetime import datetime
from uuid import UUID
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DiscoveredExtension(Base):
    """Latest presence of an extension in one device/profile/app context."""

    __tablename__ = "discovered_extension"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "workstation_fingerprint",
            "profile_ref",
            "agent_kind",
            "extension_kind",
            "extension_id",
            "scope",
            "project_ref_hash",
            name="uq_discovered_extension",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "extension_kind IN ('plugin','skill','hook','command','mcp_server')",
            name="discovered_extension_extension_kind_check",
        ),
        CheckConstraint("scope IN ('user','project')"),
        CheckConstraint("enabled IN ('true','false','unknown')"),
        CheckConstraint(
            "content_hash_status IN ('ok','too_large','unreadable')",
            name="ck_discovered_extension_hash_status",
        ),
        Index(
            "ix_discovered_extension_account_ext",
            "account_id",
            "agent_kind",
            "extension_kind",
            "extension_id",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE")
    )
    workstation_fingerprint: Mapped[str] = mapped_column(String(128))
    agent_kind: Mapped[str] = mapped_column(String(64))
    profile_ref: Mapped[str] = mapped_column(String(80), server_default="current")
    extension_kind: Mapped[str] = mapped_column(String(16))
    extension_id: Mapped[str] = mapped_column(String(128))
    version: Mapped[str | None] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(32))
    source_ref_hash: Mapped[str | None] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(16))
    project_ref_hash: Mapped[str | None] = mapped_column(String(80))
    enabled: Mapped[str] = mapped_column(String(8), server_default="unknown")
    content_hash: Mapped[str | None] = mapped_column(String(80))
    content_hash_status: Mapped[str] = mapped_column(String(16), server_default="ok")
    event: Mapped[str | None] = mapped_column(String(32))
    hook_count: Mapped[int | None]
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    id_hashed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    present: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("discovery_observation.id", ondelete="SET NULL"), index=True
    )
