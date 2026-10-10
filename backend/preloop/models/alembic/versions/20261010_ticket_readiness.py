"""Add immutable configured-policy readiness evidence; old milestones unchanged."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261010_ticket_readiness"
down_revision = "20261010_callback_receipt"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """Create additive nullable evidence contracts; no invented backfill."""
    op.create_table(
        "readiness_policy",
        *_base(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "uq_active_readiness_policy",
        "readiness_policy",
        ["account_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "readiness_observation",
        *_base(),
        sa.Column(
            "pr_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue_cost_pull_request.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("readiness_policy.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "readiness_series",
        *_base(),
        sa.Column(
            "pr_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue_cost_pull_request.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("readiness_policy.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "first_ready_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("readiness_observation.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "latest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("readiness_observation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id", "pr_id", "policy_id", name="uq_readiness_series"
        ),
    )
    op.create_table(
        "ticket_creation_evidence",
        *_base(),
        sa.Column(
            "rollup_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue_cost_rollup.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
    )
    op.create_table(
        "readiness_job",
        *_base(),
        sa.Column(
            "pr_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue_cost_pull_request.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("closed", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "readiness_cursor",
        *_base(),
        sa.Column("pr_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("account_id"),
    )
    op.create_index("ix_readiness_job_id", "readiness_job", ["id"])
    op.create_index("ix_readiness_job_account_id", "readiness_job", ["account_id"])
    op.create_index("ix_readiness_job_due_at", "readiness_job", ["due_at"])
    op.create_index("ix_readiness_cursor_id", "readiness_cursor", ["id"])
    for table in (
        "readiness_policy",
        "readiness_observation",
        "readiness_series",
        "ticket_creation_evidence",
    ):
        op.create_index(f"ix_{table}_id", table, ["id"])
        op.create_index(f"ix_{table}_account_id", table, ["account_id"])
    op.create_index(
        "ix_readiness_policy_project_id", "readiness_policy", ["project_id"]
    )
    op.create_index(
        "ix_readiness_observation_pr_id", "readiness_observation", ["pr_id"]
    )


def downgrade() -> None:
    """Remove this additive capability without touching existing intervals."""
    for table in (
        "readiness_cursor",
        "readiness_job",
        "ticket_creation_evidence",
        "readiness_series",
        "readiness_observation",
        "readiness_policy",
    ):
        op.drop_table(table)
