"""Add the delivery_report_policy table (account and per-project overrides).

Revision ID: 20261112_delivery_report_policy
Revises: 20261112_flow_log_gateway_idx
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261112_delivery_report_policy"
down_revision = "20261112_flow_log_gateway_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the table and the two partial unique indexes."""
    op.create_table(
        "delivery_report_policy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "overrides",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_delivery_report_policy_id", "delivery_report_policy", ["id"])
    op.create_index(
        "ix_delivery_report_policy_account_id",
        "delivery_report_policy",
        ["account_id"],
    )
    op.create_index(
        "uq_delivery_report_policy_account",
        "delivery_report_policy",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_delivery_report_policy_project",
        "delivery_report_policy",
        ["account_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the table; the report falls back to the built-in defaults."""
    op.drop_index("uq_delivery_report_policy_project", "delivery_report_policy")
    op.drop_index("uq_delivery_report_policy_account", "delivery_report_policy")
    op.drop_index("ix_delivery_report_policy_account_id", "delivery_report_policy")
    op.drop_index("ix_delivery_report_policy_id", "delivery_report_policy")
    op.drop_table("delivery_report_policy")
