"""Add nullable delivery receipt timestamps and the deferred claimed state.

Revision ID: 20261013_note_receipts
Revises: 20261012_note_delivery_event
"""

import sqlalchemy as sa
from alembic import op

revision = "20261013_note_receipts"
down_revision = "20261012_note_delivery_event"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS

TABLE = "agent_control_command"
CHECK = "ck_agent_control_command_status"
OLD_STATES = (
    "'pending', 'delivered', 'acked', 'failed', 'expired', 'cancelled', 'undeliverable'"
)


def upgrade() -> None:
    """Extend #1553 without duplicating its state or delivery-event column."""
    for name in ("claimed_at", "expired_at", "undeliverable_at"):
        op.add_column(TABLE, sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        TABLE, sa.Column("undeliverable_reason", sa.String(24), nullable=True)
    )
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(CHECK, TABLE, f"status IN ({OLD_STATES}, 'claimed')")


def downgrade() -> None:
    """Restore the parent schema, returning deferred claims to pending."""
    op.execute(
        sa.text(f"UPDATE {TABLE} SET status = 'pending' WHERE status = 'claimed'")
    )
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.create_check_constraint(CHECK, TABLE, f"status IN ({OLD_STATES})")
    for name in (
        "undeliverable_reason",
        "undeliverable_at",
        "expired_at",
        "claimed_at",
    ):
        op.drop_column(TABLE, name)
