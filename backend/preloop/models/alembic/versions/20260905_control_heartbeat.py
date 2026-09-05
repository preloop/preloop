"""Dedicated Agent Control heartbeat timestamp on managed agents.

Revision ID: 20260905_control_heartbeat
Revises: 20260904_flow_notifications
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "20260905_control_heartbeat"
down_revision = "20260904_flow_notifications"
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    op.add_column(
        "managed_agent",
        sa.Column(
            "control_last_heartbeat_at",
            sa.DateTime(),
            nullable=True,
            comment=(
                "Last Agent Control WebSocket heartbeat; presence for api "
                "replicas that do not hold the socket"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("managed_agent", "control_last_heartbeat_at")
