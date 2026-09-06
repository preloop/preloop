"""Self-hosted flow runner registered by the Preloop CLI."""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class FlowRunner(Base):
    """A CLI process that leases and runs flow executions.

    User/account level: who registered it, visible to the account.
    Optional instance_id when a self-hosted control plane reports through
    instance_tracker.
    """

    __tablename__ = "flow_runner"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    registered_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    instance_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    hostname: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    os: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    arch: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    labels: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="offline", index=True
    )
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    current_execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_capabilities: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )
    pending_job: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    halt_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reported_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    capabilities: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    account = relationship("Account")
    registered_by = relationship("User")

    def __repr__(self) -> str:
        return f"<FlowRunner {self.id} {self.name} {self.status}>"
