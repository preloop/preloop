"""Add flow_execution.result for structured eval result artifacts.

Revision ID: 20260815_flow_execution_result
Revises: 20260812_cost_source_reconciled
Create Date: 2026-08-15

Eval/observe flow runs report a structured result by writing
``/workspace/result.json``; the runner captures that file after the agent
finishes and persists the parsed JSON here so the API can expose it as a
first-class artifact instead of customers scraping execution logs for
DIY RESULT_JSON sentinels.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "20260815_flow_execution_result"
down_revision: Union[str, None] = "20260812_cost_source_reconciled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable JSONB result column."""
    op.add_column(
        "flow_execution",
        sa.Column("result", JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop the result column (captured artifacts are lost)."""
    op.drop_column("flow_execution", "result")
