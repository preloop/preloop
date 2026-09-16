"""Runtime session identity model for managed runtimes."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
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
        Index(
            "ix_runtime_session_account_last_activity",
            "account_id",
            "last_activity_at",
            postgresql_ops={"last_activity_at": "DESC"},
        ),
        Index(
            "ix_runtime_session_parent_session_id",
            "parent_session_id",
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
    #: The session that spawned this one, when the harness says so on the wire
    #: (see ``preloop.services.agent_session_headers``). NULL is the normal
    #: case and means "lineage unknown", never "no parent": most harnesses do
    #: not distinguish a subagent turn at all. Written once, when the row is
    #: created, and never rewritten afterwards.
    parent_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="SET NULL"),
        nullable=True,
    )
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
    #: Derived enforcement state for a legal hold, the same column shape the
    #: other held classes carry. The retention purge tests this boolean, so a
    #: held session and its activity rows survive a pass whose cutoff would
    #: otherwise take them. The account-visible record of who froze it and why
    #: is the ``legal_hold`` row, not this flag.
    legal_hold: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
        index=True,
        comment="True while a legal hold blocks this session from purge",
    )

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
