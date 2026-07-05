"""add runtime session replay run table

Revision ID: 20260705_replay_run
Revises: 20260630_tool_output_filter
Create Date: 2026-07-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260705_replay_run"
down_revision: Union[str, None] = "20260630_tool_output_filter"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the persisted replay-run table."""
    op.create_table(
        "runtime_session_replay_run",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, index=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "runtime_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runtime_session.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("suggestion_id", sa.String(80), nullable=True),
        sa.Column("candidate", JSONB(), nullable=False),
        sa.Column("n_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "input_delta_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("input_pct_saved", sa.Float(), nullable=False, server_default="0"),
        sa.Column("end_to_end_delta_median", sa.Float(), nullable=True),
        sa.Column("end_to_end_delta_low", sa.Float(), nullable=True),
        sa.Column("end_to_end_delta_high", sa.Float(), nullable=True),
        sa.Column(
            "inconclusive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("cost_spent", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
        sa.Column("consented_by", sa.String(255), nullable=True),
        sa.Column("requested_by", sa.String(255), nullable=True),
        sa.Column("result", JSONB(), nullable=False),
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
        "ix_runtime_session_replay_run_account_session",
        "runtime_session_replay_run",
        ["account_id", "runtime_session_id"],
    )


def downgrade() -> None:
    """Drop the persisted replay-run table."""
    op.drop_index(
        "ix_runtime_session_replay_run_account_session",
        table_name="runtime_session_replay_run",
    )
    op.drop_table("runtime_session_replay_run")
