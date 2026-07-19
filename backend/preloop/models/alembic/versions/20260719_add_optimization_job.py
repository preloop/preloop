"""Add async session-optimization jobs.

Revision ID: 20260719_optimization_job
Revises: 20260718_approval_bypass
Create Date: 2026-07-19

Adds:
* ``optimization_job`` — background session-optimization analysis runs the
  console submits and polls, replacing the long-blocking inline request.
  A partial unique index guarantees at most one active (pending/running)
  job per account/session pair so double-submits converge on one run.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

# revision identifiers, used by Alembic.
revision = "20260719_optimization_job"
down_revision = "20260718_approval_bypass"
branch_labels = None
depends_on = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the optimization_job table."""
    # Use postgresql.ENUM: sa.Enum ignores create_type=, which previously caused
    # CREATE TYPE to run twice (explicit create + create_table).
    optimization_job_status = ENUM(
        "pending",
        "running",
        "succeeded",
        "failed",
        name="optimization_job_status",
        create_type=False,
    )
    ENUM(
        "pending",
        "running",
        "succeeded",
        "failed",
        name="optimization_job_status",
    ).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "optimization_job",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runtime_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            optimization_job_status,
            nullable=False,
        ),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_optimization_job_id", "optimization_job", ["id"], unique=False)
    op.create_index(
        "ix_optimization_job_account_id", "optimization_job", ["account_id"]
    )
    op.create_index(
        "ix_optimization_job_runtime_session_id",
        "optimization_job",
        ["runtime_session_id"],
    )
    op.create_index(
        "ix_optimization_job_status_created_at",
        "optimization_job",
        ["status", "created_at"],
    )
    op.create_index(
        "uq_optimization_job_active",
        "optimization_job",
        ["account_id", "runtime_session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    """Drop the optimization_job table."""
    op.drop_index("uq_optimization_job_active", table_name="optimization_job")
    op.drop_index(
        "ix_optimization_job_status_created_at", table_name="optimization_job"
    )
    op.drop_index(
        "ix_optimization_job_runtime_session_id", table_name="optimization_job"
    )
    op.drop_index("ix_optimization_job_account_id", table_name="optimization_job")
    op.drop_index("ix_optimization_job_id", table_name="optimization_job")
    op.drop_table("optimization_job")

    ENUM(name="optimization_job_status").drop(op.get_bind(), checkfirst=True)
