"""Account access grants: parent-account staff working in subaccounts.

A grant gives a user or team of a parent account a level of access in all of
its subaccounts or in selected ones. The service that materializes a grant
creates ``inherited`` membership rows (``user.access_grant_id``) and the roles
that go with them (``user_role.access_grant_id``), so revoking the grant
removes exactly what it created. This repository carries the schema only
(#986); nothing here evaluates a grant.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .access_values import (
    GRANT_ACCESS_LEVELS,
    GRANT_SUBJECT_TYPES,
    GRANT_TARGET_MODES,
    in_list_check,
)
from .base import Base


# Subaccounts a ``selected`` grant applies to. Plain table: the pair is the
# whole row and the primary key.
account_access_grant_target = Table(
    "account_access_grant_target",
    Base.metadata,
    Column(
        "grant_id",
        UUID(as_uuid=True),
        ForeignKey("account_access_grant.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "subaccount_id",
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    ),
)


class AccountAccessGrant(Base):
    """Access that a parent account grants its user or team in subaccounts."""

    __tablename__ = "account_access_grant"

    parent_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        comment="The parent account issuing the grant",
    )
    subject_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="user | team"
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="user.id or team.id in the parent account",
    )
    access_level: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="read | operate | admin"
    )
    target_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="all subaccounts, or the selected ones in account_access_grant_target",
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
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            in_list_check("subject_type", GRANT_SUBJECT_TYPES),
            name="ck_account_access_grant_subject_type",
        ),
        CheckConstraint(
            in_list_check("access_level", GRANT_ACCESS_LEVELS),
            name="ck_account_access_grant_access_level",
        ),
        CheckConstraint(
            in_list_check("target_mode", GRANT_TARGET_MODES),
            name="ck_account_access_grant_target_mode",
        ),
        Index(
            "ix_account_access_grant_parent_subject",
            "parent_account_id",
            "subject_type",
            "subject_id",
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<AccountAccessGrant(id={self.id}, parent={self.parent_account_id}, "
            f"{self.subject_type}={self.subject_id}, level={self.access_level})>"
        )
