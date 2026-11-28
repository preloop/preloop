"""Add durable per-flow runner grants.

Revision ID: 20270102_runner_flow_grant
Revises: 20270102_mcp_extensions
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20270102_runner_flow_grant"
down_revision: str = "20270102_mcp_extensions"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """Create the runner_flow_grant table."""
    op.create_table(
        "runner_flow_grant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_runner.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "approval_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_request.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    for column in ("id", "account_id", "owner_user_id", "runner_id", "flow_id"):
        op.create_index(f"ix_runner_flow_grant_{column}", "runner_flow_grant", [column])
    op.create_index(
        "uq_runner_flow_grant_active",
        "runner_flow_grant",
        ["runner_id", "flow_id", "owner_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    """Drop the runner_flow_grant table."""
    op.drop_table("runner_flow_grant")
