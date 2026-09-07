"""Persist opt-in supported-release vulnerability maintenance.

Revision ID: 20260907_security_maintenance
Revises: 20260906_lifecycle_key_merge
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260907_security_maintenance"
down_revision = "20260906_lifecycle_key_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create tenant-scoped inventory, items, decisions, and baselines."""
    op.create_table(
        "security_maintenance_release",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_key", sa.String(128), nullable=False),
        sa.Column("release_key", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pinned_build_ref", sa.String(512), nullable=False),
        sa.Column("sbom_input_ref", sa.String(512), nullable=False),
        sa.Column(
            "audit_flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "implementation_flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "recheck_flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "accepted_baseline_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "approval_workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_workflow.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "approval_owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "escalation_user_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column("escalation_after_seconds", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column(
            "allowed_model_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column(
            "allowed_input_kinds",
            postgresql.ARRAY(sa.String(64)),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "product_key",
            "release_key",
            name="uq_sm_release_identity",
        ),
    )
    op.create_index(
        "ix_security_maintenance_release_id",
        "security_maintenance_release",
        ["id"],
    )
    op.create_index(
        "ix_security_maintenance_release_account_id",
        "security_maintenance_release",
        ["account_id"],
    )

    op.create_table(
        "security_maintenance_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "release_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_maintenance_release.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("product_key", sa.String(128), nullable=False),
        sa.Column("release_key", sa.String(128), nullable=False),
        sa.Column("advisory_id", sa.String(128), nullable=False),
        sa.Column("component_id", sa.String(512), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column(
            "issue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "implementation_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "recheck_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_request.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("scan_fingerprint", sa.String(64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "identity_key",
            name="uq_sm_item_identity",
        ),
    )
    op.create_index(
        "ix_security_maintenance_item_id", "security_maintenance_item", ["id"]
    )
    op.create_index(
        "ix_security_maintenance_item_account_id",
        "security_maintenance_item",
        ["account_id"],
    )
    op.create_index(
        "ix_security_maintenance_item_release_id",
        "security_maintenance_item",
        ["release_id"],
    )

    op.create_table(
        "security_maintenance_decision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_maintenance_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_request.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("evidence_ref", sa.JSON(), nullable=False),
        sa.Column("publication_ref", sa.JSON(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_security_maintenance_decision_id",
        "security_maintenance_decision",
        ["id"],
    )
    op.create_index(
        "ix_security_maintenance_decision_account_id",
        "security_maintenance_decision",
        ["account_id"],
    )
    op.create_index(
        "ix_security_maintenance_decision_item_id",
        "security_maintenance_decision",
        ["item_id"],
    )

    op.create_table(
        "security_maintenance_baseline",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "release_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_maintenance_release.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_maintenance_item.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "audit_execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(40), nullable=False),
        sa.Column("evidence_ref", sa.JSON(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_security_maintenance_baseline_id",
        "security_maintenance_baseline",
        ["id"],
    )
    op.create_index(
        "ix_security_maintenance_baseline_account_id",
        "security_maintenance_baseline",
        ["account_id"],
    )
    op.create_index(
        "ix_security_maintenance_baseline_release_id",
        "security_maintenance_baseline",
        ["release_id"],
    )
    op.create_foreign_key(
        "fk_sm_release_accepted_baseline",
        "security_maintenance_release",
        "security_maintenance_baseline",
        ["accepted_baseline_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Remove security-maintenance persistence."""
    op.drop_constraint(
        "fk_sm_release_accepted_baseline",
        "security_maintenance_release",
        type_="foreignkey",
    )
    op.drop_table("security_maintenance_baseline")
    op.drop_table("security_maintenance_decision")
    op.drop_table("security_maintenance_item")
    op.drop_table("security_maintenance_release")
