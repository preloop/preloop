"""Per-tracker-issue cost and cycle-time rollup tables (#958).

Revision ID: 20260927_issue_cost_rollup
Revises: 20260927_review_instructions
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_issue_cost_rollup"
down_revision: Union[str, None] = "20260927_review_instructions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def _base_columns() -> list[sa.Column]:
    """Columns every ``Base`` model carries."""
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """Create the issue rollup, execution fact and pull request tables."""
    op.create_table(
        "issue_cost_rollup",
        *_base_columns(),
        sa.Column(
            "tracker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracker.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issue_key", sa.String(length=512), nullable=False),
        sa.Column(
            "issue_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("issue_url", sa.String(length=1000), nullable=True),
        sa.Column("pr_url", sa.String(length=1000), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "estimated_cost",
            sa.Numeric(14, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pr_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "account_id", "tracker_id", "issue_key", name="uq_issue_cost_rollup_issue"
        ),
    )
    op.create_index("ix_issue_cost_rollup_id", "issue_cost_rollup", ["id"])
    op.create_index(
        "ix_issue_cost_rollup_account_id", "issue_cost_rollup", ["account_id"]
    )
    op.create_index(
        "ix_issue_cost_rollup_project_id", "issue_cost_rollup", ["project_id"]
    )
    op.create_index(
        "ix_issue_cost_rollup_first_event_at", "issue_cost_rollup", ["first_event_at"]
    )

    op.create_table(
        "issue_cost_execution",
        *_base_columns(),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "flow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rollup_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue_cost_rollup.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("pr_key", sa.String(length=1000), nullable=True),
        sa.Column("link", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("estimated_cost", sa.Numeric(10, 4), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_issue_cost_execution_id", "issue_cost_execution", ["id"])
    for column in ("account_id", "flow_id", "rollup_id", "project_id", "start_time"):
        op.create_index(
            f"ix_issue_cost_execution_{column}", "issue_cost_execution", [column]
        )

    op.create_table(
        "issue_cost_pull_request",
        *_base_columns(),
        sa.Column("pr_key", sa.String(length=1000), nullable=False),
        sa.Column(
            "rollup_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue_cost_rollup.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ambiguous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("account_id", "pr_key", name="uq_issue_cost_pull_request"),
    )
    op.create_index("ix_issue_cost_pull_request_id", "issue_cost_pull_request", ["id"])
    op.create_index(
        "ix_issue_cost_pull_request_account_id",
        "issue_cost_pull_request",
        ["account_id"],
    )
    op.create_index(
        "ix_issue_cost_pull_request_rollup_id",
        "issue_cost_pull_request",
        ["rollup_id"],
    )


def downgrade() -> None:
    """Drop the three rollup tables (derived data, rebuilt on demand)."""
    op.drop_table("issue_cost_pull_request")
    op.drop_table("issue_cost_execution")
    op.drop_table("issue_cost_rollup")
