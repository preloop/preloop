"""Cached optimization suggestion results for runtime sessions."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RuntimeSessionOptimizationResult(Base):
    """One cached optimization response for a session/scope/model triple.

    Persisting generated results keeps panel re-opens free; the scope hash
    encodes the request scope and model so regenerated or re-scoped requests
    produce distinct cache entries.
    """

    __tablename__ = "runtime_session_optimization_result"
    __table_args__ = (
        UniqueConstraint(
            "runtime_session_id",
            "scope_hash",
            name="uq_runtime_session_optimization_scope",
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
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<RuntimeSessionOptimizationResult(session={self.runtime_session_id}, "
            f"scope={self.scope_hash[:8]})>"
        )
