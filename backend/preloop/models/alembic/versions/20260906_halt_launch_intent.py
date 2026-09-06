"""Distinguish never-launched work from a failed launch without its reference.

Revision ID: 20260906_halt_launch_intent
Revises: 20260906_halt_artifact_merge
"""

from alembic import op
import sqlalchemy as sa

revision = "20260906_halt_launch_intent"
down_revision = "20260906_halt_artifact_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist conservative intent, including possibly launched historical rows."""
    op.add_column(
        "flow_execution", sa.Column("launch_requested_at", sa.DateTime(timezone=True))
    )
    op.execute(
        "UPDATE flow_execution SET launch_requested_at = CURRENT_TIMESTAMP WHERE status != 'PENDING' OR agent_session_reference IS NOT NULL"
    )


def downgrade() -> None:
    """Remove launch-intent metadata."""
    op.drop_column("flow_execution", "launch_requested_at")
