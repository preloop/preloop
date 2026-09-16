"""Give runners a concurrency and a row per assignment.

A runner was structurally single slot: ``flow_runner.current_execution_id``
and ``flow_runner.pending_job`` were single values, so a machine that could
comfortably run two jobs ran one and queued the rest. ``concurrency`` (default
2) says how many it may hold, and ``flow_runner_assignment`` holds one row per
job with its own payload, reported status and halt flag, so halting one job
leaves the other alone.

Existing leases are carried over: every runner with a ``current_execution_id``
gets one assignment row with its ``pending_job``, ``reported_status`` and
``halt_requested``, and only then are the old columns dropped. The downgrade
walks that back, keeping the oldest assignment per runner.

Revision ID: 20260916_runner_concurrency
Revises: 20260915_session_backfill
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260916_runner_concurrency"
down_revision: Union[str, None] = "20260915_session_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add concurrency, create the assignment table, migrate live leases."""
    op.add_column(
        "flow_runner",
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "flow_runner",
        sa.Column("reported_concurrency", sa.Integer(), nullable=True),
    )
    op.create_table(
        "flow_runner_assignment",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("runner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pending_job", postgresql.JSONB(), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "halt_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reported_status", sa.String(length=30), nullable=True),
        sa.ForeignKeyConstraint(["runner_id"], ["flow_runner.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["flow_execution.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("execution_id", name="uq_flow_runner_assignment_execution"),
    )
    op.create_index(
        op.f("ix_flow_runner_assignment_id"),
        "flow_runner_assignment",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_flow_runner_assignment_runner_id"),
        "flow_runner_assignment",
        ["runner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_flow_runner_assignment_execution_id"),
        "flow_runner_assignment",
        ["execution_id"],
        unique=False,
    )
    # Carry live leases over before the columns that hold them disappear.
    op.execute(
        sa.text(
            """
            INSERT INTO flow_runner_assignment (
                id, created_at, updated_at, runner_id, execution_id,
                pending_job, assigned_at, halt_requested, reported_status
            )
            SELECT gen_random_uuid(), now(), now(), r.id, r.current_execution_id,
                   r.pending_job, now(), r.halt_requested, r.reported_status
            FROM flow_runner AS r
            WHERE r.current_execution_id IS NOT NULL
            """
        )
    )
    op.drop_index("ix_flow_runner_current_execution_id", table_name="flow_runner")
    op.drop_column("flow_runner", "current_execution_id")
    op.drop_column("flow_runner", "pending_job")
    op.drop_column("flow_runner", "halt_requested")
    op.drop_column("flow_runner", "reported_status")


def downgrade() -> None:
    """Restore the single slot columns from the oldest assignment per runner."""
    op.add_column(
        "flow_runner",
        sa.Column("current_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "flow_runner", sa.Column("pending_job", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "flow_runner",
        sa.Column(
            "halt_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "flow_runner", sa.Column("reported_status", sa.String(length=30), nullable=True)
    )
    op.create_foreign_key(
        "flow_runner_current_execution_id_fkey",
        "flow_runner",
        "flow_execution",
        ["current_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_flow_runner_current_execution_id", "flow_runner", ["current_execution_id"]
    )
    op.execute(
        sa.text(
            """
            UPDATE flow_runner AS r
            SET current_execution_id = a.execution_id,
                pending_job = a.pending_job,
                halt_requested = a.halt_requested,
                reported_status = a.reported_status
            FROM (
                SELECT DISTINCT ON (runner_id)
                       runner_id, execution_id, pending_job, halt_requested,
                       reported_status
                FROM flow_runner_assignment
                ORDER BY runner_id, assigned_at
            ) AS a
            WHERE a.runner_id = r.id
            """
        )
    )
    op.drop_index(
        op.f("ix_flow_runner_assignment_execution_id"),
        table_name="flow_runner_assignment",
    )
    op.drop_index(
        op.f("ix_flow_runner_assignment_runner_id"), table_name="flow_runner_assignment"
    )
    op.drop_index(
        op.f("ix_flow_runner_assignment_id"), table_name="flow_runner_assignment"
    )
    op.drop_table("flow_runner_assignment")
    op.drop_column("flow_runner", "reported_concurrency")
    op.drop_column("flow_runner", "concurrency")
