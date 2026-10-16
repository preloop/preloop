"""Add the runner's self-reported discovery correlation key.

Revision ID: 20261021_runner_fingerprint
Revises: 20261020_jira_callback
"""

from alembic import op
import sqlalchemy as sa

revision = "20261021_runner_fingerprint"
down_revision = "20261020_jira_callback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Allow older runners to keep a null correlation key."""
    op.add_column(
        "flow_runner",
        sa.Column("workstation_fingerprint", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    """Remove the optional correlation key."""
    op.drop_column("flow_runner", "workstation_fingerprint")
