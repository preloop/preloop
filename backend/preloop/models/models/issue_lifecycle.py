"""Durable readiness and merge-audit operations, scoped to an issue and tenant."""

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class IssueLifecycle(Base):
    """One readiness pickup or audit per immutable merge revision."""

    __tablename__ = "issue_lifecycle"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "issue_id",
            "kind",
            "revision",
            name="uq_issue_lifecycle_revision",
        ),
    )

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE")
    )
    issue_id: Mapped[UUID] = mapped_column(ForeignKey("issue.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    revision: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="pending")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("flow_execution.id", ondelete="SET NULL"), nullable=True
    )
