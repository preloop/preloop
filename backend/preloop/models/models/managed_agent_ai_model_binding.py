"""Explicit managed-agent to AI-model association state."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .ai_model import AIModel
    from .managed_agent import ManagedAgent

# ``config_key`` prefix of bindings the model gateway creates on its own when
# an agent asks for a subscription-OAuth sibling model the onboarding import
# did not register (``gateway.autoregister.<model_identifier>``). The CLI
# never enumerates these slots, so a binding-set replacement must recognise
# them by prefix rather than expect them in the payload.
AUTOREGISTER_CONFIG_KEY_PREFIX = "gateway.autoregister."


class ManagedAgentAIModelBinding(Base):
    """Persist one managed-agent binding to one durable AI model."""

    __tablename__ = "managed_agent_ai_model_binding"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "managed_agent_id",
            "config_key",
            "gateway_alias",
            name="uq_managed_agent_ai_model_binding_slot",
        ),
    )

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
    ai_model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_model.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    binding_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="configured"
    )
    config_key: Mapped[str] = mapped_column(String(255), nullable=False)
    gateway_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="gateway_ready"
    )
    first_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False)

    managed_agent: Mapped["ManagedAgent"] = relationship(
        "ManagedAgent", back_populates="model_bindings"
    )
    ai_model: Mapped["AIModel"] = relationship(
        "AIModel", back_populates="managed_agent_bindings"
    )
