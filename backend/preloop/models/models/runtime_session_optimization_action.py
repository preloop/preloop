"""Applied optimization actions for runtime sessions."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RuntimeSessionOptimizationAction(Base):
    """One optimization suggestion action applied from the explorer.

    Persisting applied actions powers the optimization History tab and the
    measured-outcome loop: the stored ``baseline`` captures the runtime
    principal's pre-apply usage averages so later sessions can be compared
    against it.
    """

    __tablename__ = "runtime_session_optimization_action"

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
    suggestion_id: Mapped[str] = mapped_column(String(80), nullable=False)
    suggestion_title: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="applied")
    applied_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    runtime_principal_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    baseline: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    def __repr__(self) -> str:
        return (
            f"<RuntimeSessionOptimizationAction(session={self.runtime_session_id}, "
            f"type={self.action_type})>"
        )
