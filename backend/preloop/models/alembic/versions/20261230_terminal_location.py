"""Store metadata-only terminal locations on runtime sessions.

Revision ID: 20261230_terminal_location
Revises: 20261229_chat_conversations
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261230_terminal_location"
down_revision = "20261229_chat_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_session",
        sa.Column("terminal_location", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runtime_session", "terminal_location")
