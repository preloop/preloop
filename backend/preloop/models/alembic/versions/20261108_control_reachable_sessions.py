"""Native session ids an Agent Control sidecar can drive.

Revision ID: 20261108_control_reachable
Revises: 20261107_gov_capture_retry
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261108_control_reachable"
down_revision = "20261107_gov_capture_retry"
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    op.add_column(
        "managed_agent",
        sa.Column(
            "control_reachable_session_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Native session ids the connected Agent Control plugin can "
                "drive; NULL when the plugin never reported them"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("managed_agent", "control_reachable_session_ids")
