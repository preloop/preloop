"""Add assessment history and pull request identity (#2016).

Creates ``assessment`` and ``issue_cost_pull_request_issue`` and adds
``origin``, ``author_id``, ``head_sha`` and ``state`` to
``issue_cost_pull_request``. Additive; no backfill.

Revision ID: 20261110_assessment_pr_identity
Revises: 20261109_agent_usage_totals
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261110_assessment_pr_identity"
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
    """Extend the pull request record and create the two tables."""
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("origin", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.add_column("issue_cost_pull_request", sa.Column("author_id", sa.String(64)))
    op.add_column("issue_cost_pull_request", sa.Column("head_sha", sa.String(64)))
    op.add_column("issue_cost_pull_request", sa.Column("state", sa.String(16)))

    op.create_table(
        "issue_cost_pull_request_issue",
        *_base(),
        _uuid_fk("pull_request_id", "issue_cost_pull_request.id", "CASCADE", False),
        _uuid_fk("issue_id", "issue.id", "CASCADE", False),
        sa.UniqueConstraint(
            "pull_request_id", "issue_id", name="uq_issue_cost_pull_request_issue"
        ),
    )
    op.create_index(
        "ix_issue_cost_pull_request_issue_id", "issue_cost_pull_request_issue", ["id"]
    )
    op.create_index(
        "ix_issue_cost_pull_request_issue_account_id",
        "issue_cost_pull_request_issue",
        ["account_id"],
    )
    op.create_index(
        "ix_issue_cost_pull_request_issue_issue_id",
        "issue_cost_pull_request_issue",
        ["issue_id"],
    )

    op.create_table(
        "assessment",
        *_base(),
        _uuid_fk("issue_id", "issue.id", "SET NULL", True),
        _uuid_fk(
            "issue_cost_pull_request_id",
            "issue_cost_pull_request.id",
            "SET NULL",
            True,
        ),
        _uuid_fk("flow_execution_id", "flow_execution.id", "SET NULL", True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("revision", sa.String(64), nullable=True),
        sa.Column("criteria_snapshot", postgresql.JSONB(), nullable=True),
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
        sa.Column("evaluator_version", sa.String(64), nullable=True),
        sa.Column(
            "assessed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column("cost", sa.Numeric(10, 4), nullable=True),
    )
    op.create_index("ix_assessment_id", "assessment", ["id"])
    op.create_index(
        "ix_assessment_flow_execution_id", "assessment", ["flow_execution_id"]
    )
    op.create_index(
        "ix_assessment_account_issue_assessed",
        "assessment",
        ["account_id", "issue_id", sa.text("assessed_at DESC")],
    )
    op.create_index(
        "ix_assessment_account_pull_request_assessed",
        "assessment",
        ["account_id", "issue_cost_pull_request_id", sa.text("assessed_at DESC")],
    )


def downgrade() -> None:
    """Drop the two tables and the added pull request columns."""
    op.drop_table("assessment")
    op.drop_table("issue_cost_pull_request_issue")
    for column in ("state", "head_sha", "author_id", "origin"):
        op.drop_column("issue_cost_pull_request", column)
