"""One administrator-owned enforcement policy per account and agent kind."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExtensionEnforcementPolicy(Base):
    """Persist explicit marketplace sources separately from hashed discovery."""

    __tablename__ = "extension_enforcement_policy"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "agent_kind", name="uq_extension_enforcement_policy"
        ),
    )
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE")
    )
    agent_kind: Mapped[str] = mapped_column(String(64))
    marketplaces: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default="[]"
    )
    options: Mapped[dict[str, bool]] = mapped_column(JSONB, server_default="{}")
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
