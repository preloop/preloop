"""Runtime session identity model for managed runtimes."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .api_usage import ApiUsage
    from .managed_agent import ManagedAgent
    from .runtime_session_activity import RuntimeSessionActivity


class RuntimeSession(Base):
    """Shared runtime session identity across flow and non-flow runtimes."""

    __tablename__ = "runtime_session"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "session_source_type",
            "session_source_id",
            name="uq_runtime_session_account_source",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    session_source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    session_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    runtime_principal_type: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    runtime_principal_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    runtime_principal_name: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    title_request_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    account: Mapped["Account"] = relationship(
        "Account", back_populates="runtime_sessions"
    )
    api_usages: Mapped[List["ApiUsage"]] = relationship(
        "ApiUsage", back_populates="runtime_session"
    )
    activities: Mapped[List["RuntimeSessionActivity"]] = relationship(
        "RuntimeSessionActivity",
        back_populates="runtime_session",
        cascade="all, delete-orphan",
    )
    managed_agent: Mapped[Optional["ManagedAgent"]] = relationship(
        "ManagedAgent", back_populates="runtime_session", uselist=False
    )

    def __repr__(self) -> str:
        return (
            f"<RuntimeSession(id={self.id}, source={self.session_source_type}:"
            f"{self.session_source_id})>"
        )
