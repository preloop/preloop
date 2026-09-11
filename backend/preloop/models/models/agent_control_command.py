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

Two kinds of row live here (``kind``):

- ``command``: an Agent Control envelope pushed down the control WebSocket.
- ``note``: an operator note, delivered into the model conversation by the
  gateway or handed to a harness hook. Notes never travel over the control
  WebSocket, so every WebSocket query filters on ``kind == 'command'``.

Notes are immutable once written: nothing updates ``body``. Cancelling is a
state (``status='cancelled'`` plus ``cancelled_at``), never a delete, so the
record of "a human said this, then withdrew it" survives.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
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
    # Nullable since operator notes joined this table: a note can target a
    # runtime session that has no managed agent behind it (a flow execution
    # running on an account credential). Every ``command`` row still sets it.
    managed_agent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("managed_agent.id", ondelete="CASCADE"),
        nullable=True,
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

    # --- operator notes -----------------------------------------------------
    # ``command`` (the Agent Control envelope this table was built for) or
    # ``note`` (an operator note delivered into the model conversation).
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="command", default="command"
    )
    # The note text as the author typed it. Plain text on purpose: the closest
    # precedent in this schema, ``approval_request.approver_comment``, is plain
    # Text too, so there is no column-level encryption convention to follow.
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Author identity as stamped into the delivered label. Denormalised so a
    # later rename (or a deleted user) cannot rewrite what the agent was told.
    author_display: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    author_auth_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Which transport carried the note: ``gateway`` (appended to the outbound
    # model request), ``hook`` (returned to a harness hook), ``claude_channel``
    # (Claude Code channels), ``claude_message`` (Claude Code cross-session
    # inbox). NULL until delivery.
    delivery_channel: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    # How many messages the conversation held when the note landed, so the
    # timeline can show where in the run it took effect.
    delivered_turn_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The agent's next assistant turn id, when the harness reports one.
    acknowledged_turn_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    account = relationship("Account")
    managed_agent = relationship("ManagedAgent")
    runtime_session = relationship("RuntimeSession")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    cancelled_by_user = relationship("User", foreign_keys=[cancelled_by_user_id])

    __table_args__ = (
        UniqueConstraint(
            "account_id", "command_id", name="uq_agent_control_command_account_cmd"
        ),
        Index(
            "ix_agent_control_command_agent_status",
            "managed_agent_id",
            "status",
        ),
        # Every governed model call asks "is there a pending note for this
        # session or agent". These two partial indexes keep that lookup off a
        # busy agent's command history: they only cover note rows, and a
        # session with no note reads nothing.
        Index(
            "ix_agent_control_note_pending_session",
            "runtime_session_id",
            "status",
            postgresql_where=text("kind = 'note'"),
        ),
        Index(
            "ix_agent_control_note_pending_agent",
            "managed_agent_id",
            "status",
            postgresql_where=text("kind = 'note'"),
        ),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'acked', 'failed', 'expired', "
            "'cancelled')",
            name="ck_agent_control_command_status",
        ),
        CheckConstraint(
            "kind IN ('command', 'note')",
            name="ck_agent_control_command_kind",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentControlCommand(id={self.id}, command_id={self.command_id}, "
            f"status={self.status})>"
        )
