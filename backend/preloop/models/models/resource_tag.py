"""User-defined tags on resources, and per-account governance of tag keys.

Tags are what access rules select on. They are separate from system metadata
such as ``managed_agent.tags`` (JSONB) and runner ``labels``, which keep their
meaning. A tag belongs to the account that owns the resource. This repository
carries the schema only (#986).
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .access_values import (
    SHARE_RESOURCE_TYPES,
    TAG_GOVERNED_BY,
    TAG_KEY_PATTERN,
    TAG_VALUE_MAX_LENGTH,
    in_list_check,
)
from .base import Base

_KEY_CHECK = f"key ~ '{TAG_KEY_PATTERN}'"


class ResourceTag(Base):
    """One ``key=value`` tag on one resource."""

    __tablename__ = "resource_tag"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        comment="Account that owns the tagged resource",
    )
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Text, so the CHECK is the one rule for both alphabet and length.
    key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Lowercase [a-z0-9._/-], 1 to 63 characters",
    )
    value: Mapped[str] = mapped_column(String(TAG_VALUE_MAX_LENGTH), nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_KEY_CHECK, name="ck_resource_tag_key"),
        CheckConstraint(
            in_list_check("resource_type", SHARE_RESOURCE_TYPES),
            name="ck_resource_tag_resource_type",
        ),
        UniqueConstraint(
            "resource_type", "resource_id", "key", name="uq_resource_tag_key"
        ),
        Index(
            "ix_resource_tag_account_lookup",
            "account_id",
            "resource_type",
            "key",
            "value",
        ),
    )


class TagKeyPolicy(Base):
    """Who governs a tag key in an account, and which values it may take."""

    __tablename__ = "tag_key_policy"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    governed_by: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="owner | parent"
    )
    allowed_values: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(Text), nullable=True, comment="NULL means any value"
    )

    __table_args__ = (
        CheckConstraint(_KEY_CHECK, name="ck_tag_key_policy_key"),
        CheckConstraint(
            in_list_check("governed_by", TAG_GOVERNED_BY),
            name="ck_tag_key_policy_governed_by",
        ),
        UniqueConstraint("account_id", "key", name="uq_tag_key_policy_account_key"),
    )
