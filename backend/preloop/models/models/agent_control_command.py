"""Durable Agent Control command persistence for reconnect redelivery.

``AgentControlCommand`` stores every operator command envelope routed through
Agent Control before any delivery attempt, so agents that reconnect after
downtime can recover missed instructions and delivery can be audited
end-to-end.

State machine (``status``):

- ``pending``: persisted, not yet sent over a live WebSocket.
- ``delivered``: written to a connected agent's WebSocket (locally or by the
  pod holding the connection after a NATS fan-out).
- ``acked``: the runtime plugin confirmed receipt of the command id.
- ``failed``: no delivery channel was available (see ``last_error``).
- ``expired``: still pending past ``expires_at``; never redelivered.

Backward compatibility: runtime plugins that never send acks simply leave
commands in ``delivered`` state — that is expected and harmless. Redelivery
resends the original envelope verbatim (same ``message_id``/``command_id``)
so idempotent plugins can dedupe replays.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class AgentControlCommand(Base):
    """One persisted operator command envelope sent through Agent Control."""

    __tablename__ = "agent_control_command"

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
    runtime_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The envelope message_id (uuid4 string) — unique per account so acks and
    # delivery marks can be resolved by (account_id, command_id).
    command_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Full command envelope exactly as sent, so redelivery is verbatim.
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    account = relationship("Account")
    managed_agent = relationship("ManagedAgent")
    runtime_session = relationship("RuntimeSession")
    created_by_user = relationship("User")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "command_id", name="uq_agent_control_command_account_cmd"
        ),
        Index(
            "ix_agent_control_command_agent_status",
            "managed_agent_id",
            "status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentControlCommand(id={self.id}, command_id={self.command_id}, "
            f"status={self.status})>"
        )
