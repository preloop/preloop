"""Add bounded descriptive extension metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20270101_extension_details"
down_revision = "20261231_subagent_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store optional kind-specific details without changing older rows."""
    op.add_column(
        "discovered_extension", sa.Column("details", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    """Remove descriptive metadata while retaining extension identity."""
    op.drop_column("discovered_extension", "details")
