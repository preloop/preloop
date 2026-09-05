"""Durable registry entry for an onboarded external agent."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .managed_agent_ai_model_binding import ManagedAgentAIModelBinding
    from .managed_agent_credential import ManagedAgentCredential
    from .managed_agent_enrollment import ManagedAgentEnrollment
    from .runtime_session import RuntimeSession
    from .user import User


class ManagedAgent(Base):
    """Account-scoped durable record for an onboarded external agent."""

    __tablename__ = "managed_agent"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "session_source_type",
            "session_source_id",
            name="uq_managed_agent_account_source",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    owner_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    session_source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    session_source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    enrollment_hostname: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    identity_derivation: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enrolled_via: Mapped[str] = mapped_column(
        String(64), nullable=False, default="runtime_session_token"
    )
    managed_mcp_servers: Mapped[List[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    tags: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    lifecycle_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lifecycle_updated_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    control_session_mode: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )

    account: Mapped["Account"] = relationship(
        "Account", back_populates="managed_agents"
    )
    owner_user: Mapped[Optional["User"]] = relationship("User")
    runtime_session: Mapped[Optional["RuntimeSession"]] = relationship(
        "RuntimeSession", back_populates="managed_agent"
    )
    credentials: Mapped[List["ManagedAgentCredential"]] = relationship(
        "ManagedAgentCredential",
        back_populates="managed_agent",
        cascade="all, delete-orphan",
    )
    enrollments: Mapped[List["ManagedAgentEnrollment"]] = relationship(
        "ManagedAgentEnrollment",
        back_populates="managed_agent",
        cascade="all, delete-orphan",
    )
    model_bindings: Mapped[List["ManagedAgentAIModelBinding"]] = relationship(
        "ManagedAgentAIModelBinding",
        back_populates="managed_agent",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<ManagedAgent(id={self.id}, source={self.session_source_type}:"
            f"{self.session_source_id})>"
        )
