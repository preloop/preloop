"""Bind external chat addresses to shared hosted conversations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261229_chat_conversations"
down_revision = "20261230_ext_enforcement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Reserve chat addresses durably without changing existing conversation owners."""
    op.add_column(
        "flow_conversation",
        sa.Column("origin", sa.String(20), nullable=False, server_default="console"),
    )
    op.create_table(
        "chat_conversation_binding",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "account_id",
            UUID(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            UUID(),
            sa.ForeignKey("chat_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(600), nullable=False),
        sa.Column("external_user_id", sa.String(256), nullable=False),
        sa.Column(
            "owner_user_id",
            UUID(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(256), nullable=False),
        sa.Column("thread_id", sa.String(256), nullable=False),
        # Reserved before the API creates the conversation, so no foreign key.
        sa.Column("conversation_id", UUID(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("connection_id", "scope"),
    )
    op.create_index(
        "ix_chat_conversation_binding_account_id",
        "chat_conversation_binding",
        ["account_id"],
    )


def downgrade() -> None:
    """Remove transport bindings and the displayed conversation origin."""
    op.drop_table("chat_conversation_binding")
    op.drop_column("flow_conversation", "origin")
