"""Add failure_category to flow_execution.

Revision ID: 20260903_failure_category
Revises: 20260902_attention_dismiss
Create Date: 2026-09-03

Terminal executions already carry a free-text error_message, which is fine for
a human reading one run and useless for the question operators actually ask:
"what class of thing is failing, and how often?" This adds a coarse,
machine-readable category from the closed vocabulary in
preloop.services.flow_failure_category (runner_conflict, model_transient,
agent_error, tool_error, timeout, cancelled, ...).

Nullable and indexed: NULL for successful/in-flight runs and for all rows that
predate the column (deliberately not backfilled — categories are derived from
live failure context, and guessing them from historical prose would launder a
heuristic into stored data). The index serves the group-by-category queries
this column exists for.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260903_failure_category"
down_revision = "20260902_attention_dismiss"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable, indexed failure_category column."""
    op.add_column(
        "flow_execution",
        sa.Column(
            "failure_category",
            sa.String(32),
            nullable=True,
            comment="Coarse failure class for a terminal execution",
        ),
    )
    op.create_index(
        "ix_flow_execution_failure_category",
        "flow_execution",
        ["failure_category"],
    )


def downgrade() -> None:
    """Drop the failure_category column and its index."""
    op.drop_index("ix_flow_execution_failure_category", table_name="flow_execution")
    op.drop_column("flow_execution", "failure_category")
