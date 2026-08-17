"""Two-toggle notification preferences (need-me vs work-finished).

Revision ID: 20260818_notify_toggles
Revises: 20260817_add_flow_runners
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op

revision = "20260818_notify_toggles"
down_revision = "20260817_add_flow_runners"
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column(
            "notify_when_needed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Push when the agent needs the operator (approvals, ask_user)",
        ),
    )
    op.add_column(
        "notification_preferences",
        sa.Column(
            "notify_when_finished",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="Push when work finishes or stalls",
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_preferences", "notify_when_finished")
    op.drop_column("notification_preferences", "notify_when_needed")
