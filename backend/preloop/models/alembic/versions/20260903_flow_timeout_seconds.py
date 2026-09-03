"""Add timeout_seconds to flow.

Revision ID: 20260903_flow_timeout
Revises: 20260903_failure_category
Create Date: 2026-09-03

Every flow execution shared one global wall-clock ceiling
(FLOW_EXECUTION_MAX_WAIT_SECONDS, 3600). On staging that produced 7 timeout
failures all sitting exactly on the ceiling, which cannot distinguish "this
run was stuck" from "this work legitimately takes longer than an hour": a
pull-request review that should finish in minutes and a nightly security
audit that runs for two hours were held to the same number.

This adds a nullable per-flow budget. NULL keeps the deployment default, so
existing rows change behaviour in no way. The orchestrator clamps the value
into [60, 86400] at read time (see FLOW_TIMEOUT_SECONDS_MIN/MAX) rather than
with a CHECK constraint, so that a tightening of the bounds later does not
need a data migration and an out-of-range value degrades to a clamped run
instead of a write failure.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260903_flow_timeout"
down_revision = "20260903_failure_category"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable per-flow timeout budget column."""
    op.add_column(
        "flow",
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            nullable=True,
            comment=(
                "Wall-clock budget for one execution, in seconds. "
                "NULL uses the deployment default."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the per-flow timeout budget column."""
    op.drop_column("flow", "timeout_seconds")
