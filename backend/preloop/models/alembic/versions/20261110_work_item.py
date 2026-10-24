"""Add work item, work item PR and assessment tables (#2016).

Revision ID: 20261110_work_item
Revises: 20261109_agent_usage_totals
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261110_work_item"
down_revision = "20261109_agent_usage_totals"
branch_labels = None
depends_on = None


def _uuid_fk(name: str, target: str, ondelete: str, nullable: bool) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
    )


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        _uuid_fk("account_id", "account.id", "CASCADE", False),
    ]


def upgrade() -> None:
    """Create the three additive tables; no backfill."""
    op.create_table(
        "work_item",
        *_base(),
        _uuid_fk("issue_id", "issue.id", "SET NULL", True),
        _uuid_fk("flow_execution_id", "flow_execution.id", "SET NULL", True),
        _uuid_fk("flow_thread_id", "flow_thread.id", "SET NULL", True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("expected_artifact", sa.String(32), nullable=True),
        _uuid_fk("responsible_user_id", "user.id", "SET NULL", True),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("closed_reason", sa.String(64), nullable=True),
        sa.Column("closed_by", sa.String(255), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("latest_assessment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_work_item_id", "work_item", ["id"])
    op.create_index("ix_work_item_account_state", "work_item", ["account_id", "state"])
    op.create_index("ix_work_item_flow_thread", "work_item", ["flow_thread_id"])
    op.create_index("ix_work_item_issue_id", "work_item", ["issue_id"])
    op.create_index(
        "ix_work_item_flow_execution_id", "work_item", ["flow_execution_id"]
    )

    op.create_table(
        "work_item_pr",
        *_base(),
        _uuid_fk("work_item_id", "work_item.id", "CASCADE", False),
        _uuid_fk(
            "issue_cost_pull_request_id",
            "issue_cost_pull_request.id",
            "SET NULL",
            True,
        ),
        sa.Column("repo", sa.String(512), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("merged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merger", sa.String(255), nullable=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etag", sa.String(255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "work_item_id", "repo", "pr_number", name="uq_work_item_pr_number"
        ),
    )
    op.create_index("ix_work_item_pr_id", "work_item_pr", ["id"])
    op.create_index("ix_work_item_pr_repo_state", "work_item_pr", ["repo", "state"])
    op.create_index("ix_work_item_pr_account_id", "work_item_pr", ["account_id"])
    op.create_index("ix_work_item_pr_work_item_id", "work_item_pr", ["work_item_id"])
    op.create_index(
        "ix_work_item_pr_issue_cost_pull_request_id",
        "work_item_pr",
        ["issue_cost_pull_request_id"],
    )

    op.create_table(
        "assessment",
        *_base(),
        _uuid_fk("work_item_id", "work_item.id", "SET NULL", True),
        _uuid_fk("report_run_id", "flow_execution.id", "SET NULL", True),
        sa.Column("purpose_verdict", sa.String(16), nullable=False),
        sa.Column(
            "risk_flags",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("cost", sa.Numeric(10, 4), nullable=True),
    )
    op.create_index("ix_assessment_id", "assessment", ["id"])
    op.create_index("ix_assessment_account_id", "assessment", ["account_id"])
    op.create_index("ix_assessment_work_item_id", "assessment", ["work_item_id"])
    op.create_index("ix_assessment_report_run_id", "assessment", ["report_run_id"])

    op.create_foreign_key(
        "fk_work_item_latest_assessment",
        "work_item",
        "assessment",
        ["latest_assessment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop the tables; nothing else depends on them yet."""
    op.drop_constraint(
        "fk_work_item_latest_assessment", "work_item", type_="foreignkey"
    )
    op.drop_table("assessment")
    op.drop_table("work_item_pr")
    op.drop_table("work_item")
