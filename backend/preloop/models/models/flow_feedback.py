"""Durable per-PR implementation subscriptions and feedback inbox."""

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class FlowThread(Base):
    """One implementation conversation, independent from execution lifetimes."""

    __tablename__ = "flow_thread"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "flow_id",
            "tracker_id",
            "repository_id",
            "pr_number",
            name="uq_flow_thread_binding",
        ),
    )
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("account.id", ondelete="CASCADE"), nullable=False
    )
    flow_id = Column(
        UUID(as_uuid=True), ForeignKey("flow.id", ondelete="CASCADE"), nullable=False
    )
    tracker_id = Column(
        UUID(as_uuid=True), ForeignKey("tracker.id", ondelete="CASCADE"), nullable=False
    )
    repository_id = Column(String, nullable=False)
    pr_number = Column(String, nullable=False)
    pr_url = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    branch = Column(String, nullable=False)
    context = Column(JSON, nullable=False, default=dict)
    policy = Column(JSON, nullable=False, default=dict)
    state = Column(String, nullable=False, default="waiting")
    stop_reason = Column(String, nullable=True)
    latest_execution_id = Column(UUID(as_uuid=True), nullable=False)
    active_execution_id = Column(UUID(as_uuid=True), nullable=True)
    head_sha = Column(String, nullable=True)
    turns = Column(Integer, nullable=False, default=0)
    cost = Column(Float, nullable=False, default=0)
    no_progress = Column(Integer, nullable=False, default=0)
    cursor = Column(JSON, nullable=False, default=dict)
    lease_token = Column(UUID(as_uuid=True), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    due_at = Column(DateTime, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)


class FlowFeedback(Base):
    """Provider deliveries survive an active run or a worker crash."""

    __tablename__ = "flow_feedback"
    __table_args__ = (
        UniqueConstraint("thread_id", "event_key", name="uq_flow_feedback_delivery"),
    )
    thread_id = Column(
        UUID(as_uuid=True),
        ForeignKey("flow_thread.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_key = Column(String, nullable=False)
    delivery_id = Column(String, nullable=True)
    head_sha = Column(String, nullable=True)
    kind = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)
    consumed_by = Column(UUID(as_uuid=True), nullable=True)
