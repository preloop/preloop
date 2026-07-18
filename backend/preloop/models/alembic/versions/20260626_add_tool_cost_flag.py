"""add tool cost flag table

Revision ID: 20260626_tool_cost_flag
Revises: 20260611_session_opt_action
Create Date: 2026-06-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260626_tool_cost_flag"
down_revision: Union[str, None] = "20260611_session_opt_action"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the persistent tool cost flag table."""
    op.create_table(
        "tool_cost_flag",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, index=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("tool_source", sa.String(32), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("flag_kind", sa.String(64), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("estimated_weekly_cost", sa.Float(), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "disable_eligible",
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
        "uq_tool_cost_flag_account_tool_window",
        "tool_cost_flag",
        ["account_id", "tool_name", "window_start"],
        unique=True,
    )
    op.create_index(
        "ix_tool_cost_flag_account_status",
        "tool_cost_flag",
        ["account_id", "status"],
    )


def downgrade() -> None:
    """Drop the persistent tool cost flag table."""
    op.drop_index("ix_tool_cost_flag_account_status", table_name="tool_cost_flag")
    op.drop_index("uq_tool_cost_flag_account_tool_window", table_name="tool_cost_flag")
    op.drop_table("tool_cost_flag")
