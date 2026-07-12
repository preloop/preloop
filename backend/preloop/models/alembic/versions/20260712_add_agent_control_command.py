"""Add durable Agent Control command persistence table.

Revision ID: 20260712_agent_control_cmd
Revises: 20260712_provider_billing
Create Date: 2026-07-12

Persists operator command envelopes before delivery so reconnecting agents
can recover missed instructions and delivery/acks can be audited.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260712_agent_control_cmd"
down_revision: Union[str, None] = "20260712_provider_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the agent_control_command table."""
    op.create_table(
        "agent_control_command",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "managed_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("managed_agent.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_session.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("command_id", sa.String(64), nullable=False),
        sa.Column("envelope", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id", "command_id", name="uq_agent_control_command_account_cmd"
        ),
    )
    op.create_index(
        "ix_agent_control_command_account_id",
        "agent_control_command",
        ["account_id"],
    )
    op.create_index(
        "ix_agent_control_command_managed_agent_id",
        "agent_control_command",
        ["managed_agent_id"],
    )
    op.create_index(
        "ix_agent_control_command_agent_status",
        "agent_control_command",
        ["managed_agent_id", "status"],
    )


def downgrade() -> None:
    """Drop the agent_control_command table."""
    op.drop_table("agent_control_command")
