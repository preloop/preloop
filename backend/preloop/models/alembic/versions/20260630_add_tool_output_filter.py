"""add tool output filter table

Revision ID: 20260630_tool_output_filter
Revises: 20260630_budget_notify
Create Date: 2026-06-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260630_tool_output_filter"
down_revision: Union[str, None] = "20260630_approval_agent_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the tool output filter table."""
    op.create_table(
        "tool_output_filter",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, index=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("server_name", sa.String(255), nullable=True),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column(
            "managed_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("managed_agent.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("dropped_fields", JSONB(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_tool_output_filter_account_id",
        "tool_output_filter",
        ["account_id"],
    )
    op.create_index(
        "ix_tool_output_filter_account_tool",
        "tool_output_filter",
        ["account_id", "tool_name"],
    )
    op.create_index(
        "ix_tool_output_filter_managed_agent_id",
        "tool_output_filter",
        ["managed_agent_id"],
    )


def downgrade() -> None:
    """Drop the tool output filter table."""
    op.drop_index(
        "ix_tool_output_filter_managed_agent_id",
        table_name="tool_output_filter",
    )
    op.drop_index(
        "ix_tool_output_filter_account_tool",
        table_name="tool_output_filter",
    )
    op.drop_index(
        "ix_tool_output_filter_account_id",
        table_name="tool_output_filter",
    )
    op.drop_table("tool_output_filter")
