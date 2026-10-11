"""Remote harness session hosted by a personal runner (#1482, contract C)."""

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

#: States in which a session still holds a slot on its runner.
RUNNER_SESSION_LIVE_STATES = ("requested", "starting", "idle", "running", "stopping")


class RunnerRemoteSession(Base):
    """One session a runner hosts for an actor.

    ``id`` is the ``remote_session_id`` on the runner websocket. The paired
    ``RuntimeSession`` (``session_source_type="runner_session"``) carries the
    timeline. ``workspace`` never holds a clone credential, and the pending
    prompt columns are cleared as soon as the runner has acted on them.
    """

    __tablename__ = "runner_remote_sessions"
    __table_args__ = (
        Index("ix_runner_remote_sessions_runner_state", "runner_id", "state"),
        Index("ix_runner_remote_sessions_account_state", "account_id", "state"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    runtime_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runtime_session.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    runner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_runner.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    harness: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    workspace: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    workspace_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="requested")
    end_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    harness_session_id: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True
    )
    idle_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1800
    )
    #: Sent with session_start; cleared once the runner reports the start.
    first_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The one queued operator turn ``{"turn_id", "text"}``; cleared when the
    #: runner reports that turn done.
    pending_turn: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    pending_turn_sent_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    active_turn_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    stop_mode: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    start_sent_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    @property
    def is_live(self) -> bool:
        """True while the session still holds a slot on its runner."""
        return self.state in RUNNER_SESSION_LIVE_STATES

    def __repr__(self) -> str:
        return f"<RunnerRemoteSession {self.id} {self.harness} {self.state}>"
