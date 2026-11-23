"""Durable hosted flow conversations and messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261228_flow_conversations"
down_revision = "20261227_chat_flows"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    """Match the shared model identity and timestamp columns."""
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    """Create conversation storage without changing existing flows."""
    op.create_table(
        "flow_conversation",
        *timestamps(),
        sa.Column(
            "account_id",
            UUID(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "flow_id",
            UUID(),
            sa.ForeignKey("flow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            UUID(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            UUID(),
            sa.ForeignKey("flow_execution.id", ondelete="SET NULL"),
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("notice", sa.Text(), nullable=False),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("max_turns", sa.Integer(), nullable=False),
        sa.Column("idle_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("idle_deadline", sa.DateTime(timezone=True)),
        sa.Column("end_requested", sa.Boolean(), nullable=False),
        sa.Column("stop_reason", sa.String(40)),
    )
    for name in ("account_id", "flow_id"):
        op.create_index(f"ix_flow_conversation_{name}", "flow_conversation", [name])
    op.create_table(
        "flow_conversation_message",
        *timestamps(),
        sa.Column(
            "conversation_id",
            UUID(),
            sa.ForeignKey("flow_conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", UUID(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "execution_id",
            UUID(),
            sa.ForeignKey("flow_execution.id", ondelete="SET NULL"),
        ),
        sa.UniqueConstraint("conversation_id", "turn", name="uq_conversation_turn"),
        sa.UniqueConstraint(
            "conversation_id", "request_id", name="uq_conversation_request"
        ),
    )
    op.create_index(
        "ix_flow_conversation_message_conversation_id",
        "flow_conversation_message",
        ["conversation_id"],
    )


def downgrade() -> None:
    """Remove conversation storage."""
    op.drop_table("flow_conversation_message")
    op.drop_table("flow_conversation")
