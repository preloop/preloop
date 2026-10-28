"""Persist cross-process execution realtime sequence allocation.

Revision ID: 20261120_execution_realtime_seq
Revises: 20261113_pull_request_details
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261120_execution_realtime_seq"
down_revision = "20261113_pull_request_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create lazy per-execution cursors; existing executions start at zero."""
    op.create_table(
        "execution_realtime_cursor",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lifecycle_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    """Drop realtime allocation state."""
    op.drop_table("execution_realtime_cursor")
