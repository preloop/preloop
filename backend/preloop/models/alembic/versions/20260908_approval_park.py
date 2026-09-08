"""Approval window per flow and park state on flow_execution.

An approval that has to be decided on a human timescale (hours, days) cannot
be waited out by a live container. This adds the per-flow window and the four
columns that let an execution be parked while the question is outstanding and
resumed on the decision.

Revision ID: 20260908_approval_park
Revises: 20260908_structured_answer
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_approval_park"
down_revision: Union[str, None] = "20260908_structured_answer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the per-flow approval window and per-execution park state."""
    op.add_column(
        "flow",
        sa.Column("approval_window_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("park_request_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("park_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("park_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("parked_compute_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("resume_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # The expiry sweep and the resume-on-decision lookup both query by
    # park_request_id; the sweep additionally scans parked rows by expiry.
    op.create_index(
        "ix_flow_execution_park_request_id",
        "flow_execution",
        ["park_request_id"],
    )
    op.create_index(
        "ix_flow_execution_park_expires_at",
        "flow_execution",
        ["park_expires_at"],
        postgresql_where=sa.text("status = 'WAITING_FOR_HUMAN'"),
    )
    op.create_index(
        "ix_flow_execution_resume_execution_id",
        "flow_execution",
        ["resume_execution_id"],
    )
    op.create_foreign_key(
        "fk_flow_execution_resume_execution_id",
        "flow_execution",
        "flow_execution",
        ["resume_execution_id"],
        ["id"],
    )


def downgrade() -> None:
    """Drop park state. Parked executions become plain rows with no runner."""
    op.drop_constraint(
        "fk_flow_execution_resume_execution_id",
        "flow_execution",
        type_="foreignkey",
    )
    op.drop_index("ix_flow_execution_resume_execution_id", table_name="flow_execution")
    op.drop_index("ix_flow_execution_park_expires_at", table_name="flow_execution")
    op.drop_index("ix_flow_execution_park_request_id", table_name="flow_execution")
    op.drop_column("flow_execution", "resume_execution_id")
    op.drop_column("flow_execution", "parked_compute_seconds")
    op.drop_column("flow_execution", "park_expires_at")
    op.drop_column("flow_execution", "parked_at")
    op.drop_column("flow_execution", "park_requested_at")
    op.drop_column("flow_execution", "park_request_id")
    op.drop_column("flow", "approval_window_seconds")
