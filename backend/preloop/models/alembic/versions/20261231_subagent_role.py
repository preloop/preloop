"""Keep the explicit role reported by subagent lifecycle hooks."""

import sqlalchemy as sa
from alembic import op

revision = "20261231_subagent_role"
down_revision = "20261230_terminal_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an optional role without inferring one for historical sessions."""
    op.add_column("runtime_session", sa.Column("subagent_role", sa.String(64)))


def downgrade() -> None:
    """Remove the hook-reported role."""
    op.drop_column("runtime_session", "subagent_role")
