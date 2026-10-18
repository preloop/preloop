"""Normalized runtime-session activity records."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .api_key import ApiKey
    from .flow_execution import FlowExecution
    from .runtime_session import RuntimeSession


class RuntimeSessionActivity(Base):
    """One durable activity item associated with a runtime session."""

    __tablename__ = "runtime_session_activity"
    __table_args__ = (
        # Prompt, turn-end and session-end timestamps (#1889), kept small
        # next to the tool-call volume so the live activity worker reads
        # them without scanning the table.
        Index(
            "ix_rsa_account_turn_events",
            "account_id",
            text("timestamp DESC"),
            postgresql_where=text(
                "activity_type IN ('user_prompt', 'turn_end', 'session_end')"
            ),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    flow_execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    api_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_key.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    activity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    server_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tool_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    account: Mapped["Account"] = relationship("Account")
    runtime_session: Mapped["RuntimeSession"] = relationship(
        "RuntimeSession", back_populates="activities"
    )
    flow_execution: Mapped[Optional["FlowExecution"]] = relationship("FlowExecution")
    api_key: Mapped[Optional["ApiKey"]] = relationship("ApiKey")

    def __repr__(self) -> str:
        return (
            f"<RuntimeSessionActivity(id={self.id}, runtime_session_id="
            f"{self.runtime_session_id}, activity_type={self.activity_type})>"
        )
