"""Persist control WebSocket ownership independently of runtime identity."""

from alembic import op
import sqlalchemy as sa

revision = "20260912_control_connection"
down_revision = "20260910_operator_notes"
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS


def upgrade() -> None:
    op.add_column(
        "managed_agent", sa.Column("control_connection_id", sa.UUID(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("managed_agent", "control_connection_id")
