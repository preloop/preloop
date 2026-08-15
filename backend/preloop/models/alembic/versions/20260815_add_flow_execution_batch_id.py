"""Add batch_id to flow_execution for matrix/batch fan-out triggers.

Revision ID: 20260815_flow_exec_batch_id
Revises: 20260812_cost_source_reconciled
Create Date: 2026-08-15

A matrix trigger creates one execution per (agent_type, ai_model_id) cell; all
cells share a batch_id so the batch can be listed and rolled up as a unit.
Nullable: executions created outside a batch simply have no batch_id.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260815_flow_exec_batch_id"
down_revision: Union[str, None] = "20260812_cost_source_reconciled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    op.add_column(
        "flow_execution",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_flow_execution_batch_id",
        "flow_execution",
        ["batch_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_flow_execution_batch_id", table_name="flow_execution")
    op.drop_column("flow_execution", "batch_id")
