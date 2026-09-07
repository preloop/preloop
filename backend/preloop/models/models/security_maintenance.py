"""Opt-in supported-release inventory, remediation items, and baselines."""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SecurityMaintenanceRelease(Base):
    """Explicitly opted-in product/release under vulnerability maintenance."""

    __tablename__ = "security_maintenance_release"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "product_key",
            "release_key",
            name="uq_sm_release_identity",
        ),
    )

    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_key: Mapped[str] = mapped_column(String(128), nullable=False)
    release_key: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    pinned_build_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    sbom_input_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    audit_flow_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow.id", ondelete="RESTRICT"),
        nullable=False,
    )
    implementation_flow_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recheck_flow_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow.id", ondelete="RESTRICT"),
        nullable=True,
    )
    accepted_baseline_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "security_maintenance_baseline.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_sm_release_accepted_baseline",
        ),
        nullable=True,
    )
    approval_workflow_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("approval_workflow.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_owner_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    escalation_user_ids: Mapped[list[UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    escalation_after_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=604800
    )
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    allowed_model_ids: Mapped[list[UUID] | None] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=True
    )
    allowed_input_kinds: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(64)), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SecurityMaintenanceItem(Base):
    """One durable remediation item per product+release+advisory/component."""

    __tablename__ = "security_maintenance_item"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "identity_key",
            name="uq_sm_item_identity",
        ),
    )

    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    release_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("security_maintenance_release.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False)
    product_key: Mapped[str] = mapped_column(String(128), nullable=False)
    release_key: Mapped[str] = mapped_column(String(128), nullable=False)
    advisory_id: Mapped[str] = mapped_column(String(128), nullable=False)
    component_id: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    issue_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("issue.id", ondelete="SET NULL"),
        nullable=True,
    )
    implementation_execution_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
    )
    recheck_execution_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
    )
    approval_request_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("approval_request.id", ondelete="SET NULL"),
        nullable=True,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SecurityMaintenanceDecision(Base):
    """Append-only decision history. Rows are never updated after insert."""

    __tablename__ = "security_maintenance_decision"

    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("security_maintenance_item.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    execution_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="SET NULL"),
        nullable=True,
    )
    approval_request_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("approval_request.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence_ref: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    publication_ref: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SecurityMaintenanceBaseline(Base):
    """Accepted evidence baseline. Failed or incomplete audits never insert."""

    __tablename__ = "security_maintenance_baseline"

    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    release_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("security_maintenance_release.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("security_maintenance_item.id", ondelete="SET NULL"),
        nullable=True,
    )
    audit_execution_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("flow_execution.id", ondelete="RESTRICT"),
        nullable=False,
    )
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_ref: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
