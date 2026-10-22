"""Add runtime_session.turn_state and the current turn's bounds.

Revision ID: 20261107_session_turn_state
Revises: 20261107_gov_capture_retry
"""

import sqlalchemy as sa
from alembic import op

revision = "20261107_session_turn_state"
down_revision = "20261107_gov_capture_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable column; existing sessions stay NULL (unknown)."""
    op.add_column(
        "runtime_session",
        sa.Column("turn_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "runtime_session", sa.Column("turn_started_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "runtime_session", sa.Column("turn_ended_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "runtime_session", sa.Column("turn_interrupted", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    """Drop the columns."""
    for column in ("turn_interrupted", "turn_ended_at", "turn_started_at"):
        op.drop_column("runtime_session", column)
    op.drop_column("runtime_session", "turn_state")
