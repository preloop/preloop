"""Persist readiness pickups and independent merge-audit operations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260906_issue_lifecycle"
down_revision = "20260906_flow_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create tenant-scoped, idempotent lifecycle operation storage."""
    op.create_table(
        "issue_lifecycle",
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
            "issue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="SET NULL"),
        ),
        sa.UniqueConstraint(
            "account_id",
            "issue_id",
            "kind",
            "revision",
            name="uq_issue_lifecycle_revision",
        ),
    )
    op.create_index("ix_issue_lifecycle_id", "issue_lifecycle", ["id"])


def downgrade() -> None:
    """Remove lifecycle operation storage."""
    op.drop_table("issue_lifecycle")
