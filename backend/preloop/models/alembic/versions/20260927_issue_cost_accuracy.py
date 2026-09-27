"""Add PR-opened source and tracker estimates to the issue cost rollup.

Revision ID: 20260927_issue_cost_accuracy
Revises: 20260927_policy_notice_hit
Create Date: 2026-09-27

Additive only. ``opened_at_source`` records whether a pull request's opened
time is the forge's own ``created_at``, the bind time or the run end, so the
report can say how exact "PR opened" is. The four estimate columns hold the
human estimate exactly as the tracker states it (hours and/or points, each
with the field or label it came from); NULL when the tracker has none.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260927_issue_cost_accuracy"
down_revision = "20260927_policy_notice_hit"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable source and estimate columns."""
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("opened_at_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "issue_cost_rollup",
        sa.Column("pr_opened_at_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "issue_cost_rollup",
        sa.Column("estimate_hours", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "issue_cost_rollup",
        sa.Column("estimate_hours_source", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "issue_cost_rollup",
        sa.Column("estimate_points", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "issue_cost_rollup",
        sa.Column("estimate_points_source", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    """Drop the columns added by this revision."""
    for column in (
        "estimate_points_source",
        "estimate_points",
        "estimate_hours_source",
        "estimate_hours",
        "pr_opened_at_source",
    ):
        op.drop_column("issue_cost_rollup", column)
    op.drop_column("issue_cost_pull_request", "opened_at_source")
