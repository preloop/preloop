"""Stable account-local CI identity, independent of its rotating keys."""

import uuid
from typing import Any, Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CiPrincipal(Base):
    """Disable instead of deleting: historical ownership is never reassigned."""

    __tablename__ = "ci_principal"
    __table_args__ = (
        Index("ix_ci_principal_account_binding", "account_id", "project_id", "flow_id"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    administered_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project.id", ondelete="RESTRICT"), index=True
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("flow.id", ondelete="RESTRICT"), index=True
    )
    # Snapshot repository identity, so moving a project never repoints a grant.
    tracker_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    repository_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    repository_binding: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    grant: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
