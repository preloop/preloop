"""Preserve harness launch directories and Agent Control ownership."""

from alembic import op
import sqlalchemy as sa

revision = "20261231_session_start_dir"
down_revision = "20261229_chat_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable fields; historical last cwd is not a known launch directory."""
    op.add_column(
        "runtime_session", sa.Column("start_directory", sa.String(1024), nullable=True)
    )
    op.add_column(
        "runtime_session", sa.Column("control_owned", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    """Remove launch metadata."""
    op.drop_column("runtime_session", "control_owned")
    op.drop_column("runtime_session", "start_directory")
