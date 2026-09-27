"""Attribute-based access rules layered on top of RBAC.

A rule permits or forbids actions for subjects on resources that its
selectors match (usually by tag), within the owning account, its subaccounts,
or both. This repository carries the schema only (#986); rule evaluation
ships elsewhere.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .access_values import (
    RULE_ACTIONS,
    RULE_EFFECTS,
    RULE_SCOPES,
    TAGGABLE_RESOURCE_TYPES,
    in_list_check,
)
from .base import Base

_ACTIONS_ARRAY = "ARRAY[" + ", ".join(f"'{a}'" for a in RULE_ACTIONS) + "]::text[]"


class AccessRule(Base):
    """A permit or forbid rule over actions, subjects and tagged resources."""

    __tablename__ = "access_rule"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    effect: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="permit | forbid"
    )
    actions: Mapped[List[str]] = mapped_column(ARRAY(Text), nullable=False)
    subject_selector: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    resource_type: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment="One of the taggable resource types; NULL matches every type",
    )
    resource_selector: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    conditions: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    scope: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="self",
        server_default="self",
        comment="self | subaccounts | self_and_subaccounts",
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            in_list_check("effect", RULE_EFFECTS), name="ck_access_rule_effect"
        ),
        CheckConstraint(
            in_list_check("scope", RULE_SCOPES), name="ck_access_rule_scope"
        ),
        CheckConstraint(
            "resource_type IS NULL OR "
            + in_list_check("resource_type", TAGGABLE_RESOURCE_TYPES),
            name="ck_access_rule_resource_type",
        ),
        CheckConstraint(
            f"cardinality(actions) >= 1 AND actions <@ {_ACTIONS_ARRAY}",
            name="ck_access_rule_actions",
        ),
        Index("ix_access_rule_account_enabled", "account_id", "is_enabled"),
    )
