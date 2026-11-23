"""Durable, single-owner conversations with hosted flow runtimes."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class FlowConversation(Base):
    """Identity and admission lock shared by every client and runtime turn."""

    __tablename__ = "flow_conversation"

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    flow_id: Mapped[UUID] = mapped_column(
        ForeignKey("flow.id", ondelete="CASCADE"), index=True
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE")
    )
    execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("flow_execution.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(String(20), default="starting")
    notice: Mapped[str] = mapped_column(Text, default="Starting your conversation.")
    turns: Mapped[int] = mapped_column(Integer, default=0)
    max_turns: Mapped[int] = mapped_column(Integer)
    idle_timeout_seconds: Mapped[int] = mapped_column(Integer)
    idle_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_requested: Mapped[bool] = mapped_column(default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(40))


class FlowConversationMessage(Base):
    """One admitted user turn and its durable streamed answer."""

    __tablename__ = "flow_conversation_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn", name="uq_conversation_turn"),
        UniqueConstraint(
            "conversation_id", "request_id", name="uq_conversation_request"
        ),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("flow_conversation.id", ondelete="CASCADE"), index=True
    )
    request_id: Mapped[UUID] = mapped_column()
    turn: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("flow_execution.id", ondelete="SET NULL")
    )
