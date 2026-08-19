"""Usage ingest round 2: conversation rollup columns and cost_basis.

Revision ID: 20260818_usage_ingest_conv
Revises: 20260817_add_flow_runners
Create Date: 2026-08-18

The usage push API (issue #123 evolution) rolls subagent workers billed on
separate conversations up under their parent thread, tracks message/tool
growth tripwires, and lets billing-export ("reconciled") records supersede
hook-derived ("estimated") cost without double-counting. That needs real
indexed columns instead of JSONB-only storage:

- ``conversation_id`` / ``parent_conversation_id``: worker->parent rollup,
  partially indexed for imported usage only.
- ``message_count`` / ``tool_call_count``: growth tripwires, never derived
  from tokens.
- ``cost_basis``: 'estimated' | 'reconciled'; NULL on legacy rows, which
  never participate in supersession.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "20260818_usage_ingest_conv"
down_revision: Union[str, None] = "20260817_add_flow_runners"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add rollup/tripwire/cost-basis columns and their partial indexes."""
    op.add_column(
        "api_usage", sa.Column("conversation_id", sa.String(255), nullable=True)
    )
    op.add_column(
        "api_usage", sa.Column("parent_conversation_id", sa.String(255), nullable=True)
    )
    op.add_column("api_usage", sa.Column("message_count", sa.Integer(), nullable=True))
    op.add_column(
        "api_usage", sa.Column("tool_call_count", sa.Integer(), nullable=True)
    )
    op.add_column("api_usage", sa.Column("cost_basis", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_api_usage_cost_basis",
        "api_usage",
        "cost_basis IS NULL OR cost_basis IN ('estimated', 'reconciled')",
    )
    op.create_index(
        "ix_api_usage_imported_conversation",
        "api_usage",
        ["account_id", "conversation_id"],
        postgresql_where=text(
            "action_type = 'imported_usage' AND conversation_id IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_api_usage_imported_parent_conv",
        "api_usage",
        ["account_id", "parent_conversation_id"],
        postgresql_where=text(
            "action_type = 'imported_usage' AND parent_conversation_id IS NOT NULL"
        ),
    )
    # Backfill: pre-round-2 push-ingested rows stored conversation ids in
    # meta_data only. Scoped to imported usage; gateway rows are untouched.
    op.execute(
        "UPDATE api_usage SET "
        "conversation_id = meta_data->>'conversation_id', "
        "parent_conversation_id = meta_data->>'parent_conversation_id' "
        "WHERE action_type = 'imported_usage' AND meta_data IS NOT NULL "
        "AND (meta_data ? 'conversation_id' OR meta_data ? 'parent_conversation_id')"
    )


def downgrade() -> None:
    """Drop the round-2 columns, indexes, and CHECK constraint."""
    op.drop_index("ix_api_usage_imported_parent_conv", table_name="api_usage")
    op.drop_index("ix_api_usage_imported_conversation", table_name="api_usage")
    op.drop_constraint("ck_api_usage_cost_basis", "api_usage", type_="check")
    op.drop_column("api_usage", "cost_basis")
    op.drop_column("api_usage", "tool_call_count")
    op.drop_column("api_usage", "message_count")
    op.drop_column("api_usage", "parent_conversation_id")
    op.drop_column("api_usage", "conversation_id")
