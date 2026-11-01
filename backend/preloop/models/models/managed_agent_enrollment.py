"""Durable enrollment state for managed agents."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from preloop.models.db.encrypted_types import SecretFieldsJSON

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .managed_agent import ManagedAgent
    from .user import User


class ManagedAgentEnrollment(Base):
    """Persist discovered and managed config state for one agent enrollment."""

    __tablename__ = "managed_agent_enrollment"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    managed_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("managed_agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    enrollment_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="runtime_session_bootstrap"
    )
    adapter_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    target_config_path: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    discovered_config: Mapped[dict] = mapped_column(
        SecretFieldsJSON("agent_config", jsonb=True), nullable=False, default=dict
    )
    managed_config: Mapped[dict] = mapped_column(
        SecretFieldsJSON("agent_config", jsonb=True), nullable=False, default=dict
    )
    backup_metadata: Mapped[dict] = mapped_column(
        SecretFieldsJSON("agent_config", jsonb=True), nullable=False, default=dict
    )
    validation_result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    restore_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_applied_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_validated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_restored_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    account: Mapped["Account"] = relationship(
        "Account", back_populates="managed_agent_enrollments"
    )
    managed_agent: Mapped["ManagedAgent"] = relationship(
        "ManagedAgent", back_populates="enrollments"
    )
    created_by_user: Mapped[Optional["User"]] = relationship("User")
