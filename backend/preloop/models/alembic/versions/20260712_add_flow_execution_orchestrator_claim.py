"""Add orchestrator claim/heartbeat columns to flow_execution.

Revision ID: 20260712_flow_orch_claim
Revises: 20260712_usage_perf_idx
Create Date: 2026-07-12

Enables multi-replica-safe flow orchestration on sync workers: one worker
claims an execution via DB lease, heartbeats while monitoring, and releases
on terminal status.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260712_flow_orch_claim"
down_revision: Union[str, None] = "20260712_usage_perf_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add claim lease columns for worker-owned orchestration."""
    op.add_column(
        "flow_execution",
        sa.Column(
            "orchestrator_worker_id",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.add_column(
        "flow_execution",
        sa.Column(
            "orchestrator_claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "flow_execution",
        sa.Column(
            "orchestrator_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_flow_execution_orchestrator_worker_id",
        "flow_execution",
        ["orchestrator_worker_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_flow_execution_orchestrator_heartbeat_at",
        "flow_execution",
        ["orchestrator_heartbeat_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop claim lease columns."""
    op.drop_index(
        "ix_flow_execution_orchestrator_heartbeat_at",
        table_name="flow_execution",
        if_exists=True,
    )
    op.drop_index(
        "ix_flow_execution_orchestrator_worker_id",
        table_name="flow_execution",
        if_exists=True,
    )
    op.drop_column("flow_execution", "orchestrator_heartbeat_at")
    op.drop_column("flow_execution", "orchestrator_claimed_at")
    op.drop_column("flow_execution", "orchestrator_worker_id")
