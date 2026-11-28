"""Add the flow pre-check configuration column (#2541)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20270105_flow_precheck"
down_revision = "20270105_flow_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``flow.precheck``; NULL keeps every existing flow unchanged."""
    op.add_column(
        "flow",
        sa.Column("precheck", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop ``flow.precheck``."""
    op.drop_column("flow", "precheck")
