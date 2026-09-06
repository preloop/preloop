"""Persist recovery attribution and managed execution stop intent.

Revision ID: 20260906_halt_durability
Revises: 20260906_account_halt
"""

from alembic import op
import sqlalchemy as sa

revision = "20260906_halt_durability"
down_revision = "20260906_account_halt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add durable stop and recovery fields without rewriting prior migrations."""
    op.add_column("account_halt", sa.Column("deactivation_reason", sa.String(500)))
    op.add_column(
        "flow_execution", sa.Column("stop_requested_at", sa.DateTime(timezone=True))
    )
    op.add_column("flow_execution", sa.Column("stop_reason", sa.String(500)))
    op.add_column("flow_execution", sa.Column("stop_source", sa.String(32)))
    op.add_column(
        "flow_execution", sa.Column("stop_confirmed_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    """Remove fields introduced by this revision."""
    for name in (
        "stop_confirmed_at",
        "stop_source",
        "stop_reason",
        "stop_requested_at",
    ):
        op.drop_column("flow_execution", name)
    op.drop_column("account_halt", "deactivation_reason")
