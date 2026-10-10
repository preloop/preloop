"""Resource sharing from an owner account to other accounts in its tree.

``resource_share`` is the owner's statement of intent: which resource, to all
subaccounts, to selected ones, or to those an access rule selects.
``resource_share_recipient`` is the materialized result, one row per account
that can see the resource. Hot paths read only the recipient table, through a
join on ``(recipient_account_id, resource_type)``; nothing walks the tree per
request. This repository carries the schema only (#986); the materializer
ships elsewhere.

A recipient row exists only while its share is live, enforced by two
database triggers (revision ``20260928_share_tag_rule``): setting
``resource_share.revoked_at`` deletes the share's recipient rows, and a
revoked share cannot gain new ones. Readers of the recipient table therefore
never see a revoked share, whatever the service does or how late it runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .access_values import SHARE_RESOURCE_TYPES, SHARE_TARGET_MODES, in_list_check
from .base import Base


class ResourceShare(Base):
    """An owner account sharing one resource with accounts in its tree."""

    __tablename__ = "resource_share"

    owner_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        comment="Account that owns the shared resource",
    )
    resource_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="ai_model | mcp_server | managed_agent | flow | runner_pool | policy_baseline",
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="all | selected | rule"
    )
    access_rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_rule.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Rule selecting recipients; set exactly when target_mode is rule",
    )
    selected_account_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    is_automatic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    require_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            in_list_check("resource_type", SHARE_RESOURCE_TYPES),
            name="ck_resource_share_resource_type",
        ),
        CheckConstraint(
            in_list_check("target_mode", SHARE_TARGET_MODES),
            name="ck_resource_share_target_mode",
        ),
        CheckConstraint(
            "(target_mode = 'rule') = (access_rule_id IS NOT NULL)",
            name="ck_resource_share_rule_mode",
        ),
        Index(
            "ix_resource_share_owner_resource",
            "owner_account_id",
            "resource_type",
            "resource_id",
        ),
    )


class ResourceShareRecipient(Base):
    """One account that can see a shared resource (materialized)."""

    __tablename__ = "resource_share_recipient"

    share_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resource_share.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recipient_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        comment="Copied from the share so the hot-path join needs no second table",
    )
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "recipient_account_id",
            "resource_type",
            "resource_id",
            "share_id",
            name="uq_resource_share_recipient",
        ),
        Index(
            "ix_resource_share_recipient_type",
            "recipient_account_id",
            "resource_type",
        ),
        CheckConstraint(
            in_list_check("resource_type", SHARE_RESOURCE_TYPES),
            name="ck_resource_share_recipient_resource_type",
        ),
    )
