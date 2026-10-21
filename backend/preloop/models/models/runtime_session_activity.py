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
        # Command/result deduplication must not scan unrelated activity payloads.
        Index(
            "ix_rsa_account_command_lookup",
            "account_id",
            text("(metadata ->> 'command_id')"),
            text("timestamp DESC"),
            postgresql_where=text("(metadata ->> 'command_id') IS NOT NULL"),
        ),
        # Superset of the PR-only index for bounded Fleet Map work history.
        Index(
            "ix_runtime_session_activity_work_tools",
            "account_id",
            text("timestamp DESC"),
            postgresql_include=["tool_name", "flow_execution_id", "runtime_session_id"],
            postgresql_where=text(
                "activity_type = 'tool_call' AND status = 'succeeded' AND "
                "tool_name IN ('add_comment', 'add_comment_to_pending_review', "
                "'approve_merge_request', 'create_merge_request', "
                "'create_merge_request_note', 'create_pull_request', "
                "'create_pull_request_review', 'merge_merge_request', "
                "'merge_pull_request', 'pull_request_review_write', "
                "'submit_pending_pull_request_review', 'update_pull_request', "
                "'create_issue', 'update_issue', 'update_merge_request')"
            ),
        ),
        # Classified work calls (metadata ``work_ref``: built-in tools, GitHub
        # and GitLab MCP, gh/glab via native hooks). Migration
        # 20261106_fleet_work_ref_idx; statuses match WORK_REF_STATUSES.
        Index(
            "ix_runtime_session_activity_work_ref",
            "account_id",
            text("timestamp DESC"),
            postgresql_include=["tool_name", "flow_execution_id", "runtime_session_id"],
            postgresql_where=text(
                "activity_type = 'tool_call' AND metadata ? 'work_ref' AND "
                "status IN ('succeeded', 'allowed', 'approved')"
            ),
        ),
        # Prompt, turn-end and session-end timestamps (#1889), kept small
        # next to the tool-call volume. Timestamp first: the live activity
        # worker reads them across every account by time range.
        Index(
            "ix_rsa_account_turn_events",
            "timestamp",
            "account_id",
            postgresql_where=text(
                "activity_type IN ('user_prompt', 'turn_end', 'session_end')"
            ),
        ),
        # Fleet Map code activity: successful pull/merge request tool calls,
        # newest first per account. Migration 20261031_code_tool_idx; the
        # tool list matches preloop.models.crud.fleet_map.CODE_ACTIVITY_TOOLS.
        Index(
            "ix_runtime_session_activity_code_tools",
            "account_id",
            text("timestamp DESC"),
            postgresql_include=["tool_name", "flow_execution_id", "runtime_session_id"],
            postgresql_where=text(
                "activity_type = 'tool_call' AND status = 'succeeded' AND "
                "tool_name IN ('add_comment', 'add_comment_to_pending_review', "
                "'approve_merge_request', "
                "'create_merge_request', 'create_merge_request_note', "
                "'create_pull_request', "
                "'create_pull_request_review', 'merge_merge_request', "
                "'merge_pull_request', 'pull_request_review_write', "
                "'submit_pending_pull_request_review', 'update_pull_request')"
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
