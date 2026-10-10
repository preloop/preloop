"""Account-scoped immutable evidence and first-ready policy series."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ReadinessPolicyRecord(Base):
    """Immutable project policy configuration with audited activation."""

    __tablename__ = "readiness_policy"
    __table_args__ = (
        Index(
            "uq_active_readiness_policy",
            "account_id",
            "project_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ReadinessObservationRecord(Base):
    """Compact gate evidence, never provider credentials or full responses."""

    __tablename__ = "readiness_observation"
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pr_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("issue_cost_pull_request.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    policy_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("readiness_policy.id", ondelete="CASCADE"),
        nullable=True,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ReadinessSeries(Base):
    """First ready stays fixed; latest state can regress or become stale."""

    __tablename__ = "readiness_series"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "pr_id", "policy_id", name="uq_readiness_series"
        ),
    )
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pr_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("issue_cost_pull_request.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("readiness_policy.id", ondelete="CASCADE"),
        nullable=False,
    )
    first_ready_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("readiness_observation.id", ondelete="SET NULL"),
        nullable=True,
    )
    latest_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("readiness_observation.id", ondelete="CASCADE"),
        nullable=False,
    )


class TicketCreationRecord(Base):
    """Tracker-authoritative creation field with retrieval provenance."""

    __tablename__ = "ticket_creation_evidence"
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rollup_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("issue_cost_rollup.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)


class ReadinessJob(Base):
    """One durable deduplicated lease per bound pull request."""

    __tablename__ = "readiness_job"
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pr_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("issue_cost_pull_request.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_token: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    closed: Mapped[bool] = mapped_column(default=False, nullable=False)


class ReadinessCursor(Base):
    """Round-robin reconciliation position, isolated to one account."""

    __tablename__ = "readiness_cursor"
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    pr_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
