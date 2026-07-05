"""Persisted re-execution replay runs for session-optimization savings."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RuntimeSessionReplayRun(Base):
    """One re-execution replay of a session request to measure savings.

    A replay re-runs a stored request twice per pass (original and
    candidate-modified) ``n_runs`` times against the upstream model and records
    both the deterministic input-token delta (exact, the headline number) and
    the noise-banded end-to-end delta. Because a replay spends the user's real
    budget and re-sends their stored payload upstream, ``consented_by`` records
    the explicit per-run consent, and ``status`` distinguishes a completed run
    from one aborted by the hard budget cap or skipped for a missing payload.
    """

    __tablename__ = "runtime_session_replay_run"

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
    suggestion_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    candidate: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    n_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_delta_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_pct_saved: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    end_to_end_delta_median: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    end_to_end_delta_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    end_to_end_delta_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    inconclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cost_spent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    consented_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    requested_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    def __repr__(self) -> str:
        return (
            f"<RuntimeSessionReplayRun(session={self.runtime_session_id}, "
            f"status={self.status}, inconclusive={self.inconclusive})>"
        )
