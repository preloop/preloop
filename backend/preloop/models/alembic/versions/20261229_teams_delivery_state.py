"""Remember validated Teams personal delivery addresses.

Revision ID: 20261229_teams_delivery
Revises: 20261228_flow_conversations
"""

from alembic import op
import sqlalchemy as sa

revision = "20261229_teams_delivery"
down_revision = "20261228_flow_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable state; existing identities remain valid."""
    op.add_column(
        "chat_identity", sa.Column("provider_state", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    """Remove only transport state."""
    op.drop_column("chat_identity", "provider_state")
