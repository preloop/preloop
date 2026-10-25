"""Stored Delivery Report policy: one account row, optional project rows.

The report policy itself (fields, defaults, validation) is
:class:`preloop.services.delivery_report.DeliveryReportPolicy`. This table
stores only what an account or a project changed from those defaults, as a
partial JSON document, so a default that moves in a later release reaches
every account that never touched it. ``project_id`` is null on the
account row; a project row overrides the account row for that repository.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DeliveryReportPolicySetting(Base):
    """What an account, or one of its projects, set on the report policy."""

    __tablename__ = "delivery_report_policy"
    __table_args__ = (
        Index(
            "uq_delivery_report_policy_account",
            "account_id",
            unique=True,
            postgresql_where=text("project_id IS NULL"),
        ),
        Index(
            "uq_delivery_report_policy_project",
            "account_id",
            "project_id",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=True,
    )
    #: Only the fields that differ from the defaults (account row) or from
    #: the account row (project row).
    overrides: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
