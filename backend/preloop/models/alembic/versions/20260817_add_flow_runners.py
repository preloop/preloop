"""Add flow_runner table and runner_pool / runner_id columns.

Revision ID: 20260817_add_flow_runners
Revises: 20260815_flow_schedule_config
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260817_add_flow_runners"
down_revision: Union[str, None] = "20260815_flow_schedule_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "flow",
        sa.Column("runner_pool", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("runner_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_flow_execution_runner_id", "flow_execution", ["runner_id"])

    op.create_table(
        "flow_runner",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "registered_by_user_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("os", sa.String(length=30), nullable=True),
        sa.Column("arch", sa.String(length=30), nullable=True),
        sa.Column(
            "labels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="offline"
        ),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("pending_job", postgresql.JSONB(), nullable=True),
        sa.Column(
            "halt_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reported_status", sa.String(length=30), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["registered_by_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["current_execution_id"], ["flow_execution.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_flow_runner_account_id", "flow_runner", ["account_id"])
    op.create_index("ix_flow_runner_status", "flow_runner", ["status"])
    op.create_index("ix_flow_runner_last_heartbeat", "flow_runner", ["last_heartbeat"])
    op.create_index(
        "ix_flow_runner_current_execution_id", "flow_runner", ["current_execution_id"]
    )

    op.create_foreign_key(
        "fk_flow_execution_runner_id",
        "flow_execution",
        "flow_runner",
        ["runner_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_flow_execution_runner_id", "flow_execution", type_="foreignkey"
    )
    op.drop_index("ix_flow_runner_current_execution_id", table_name="flow_runner")
    op.drop_index("ix_flow_runner_last_heartbeat", table_name="flow_runner")
    op.drop_index("ix_flow_runner_status", table_name="flow_runner")
    op.drop_index("ix_flow_runner_account_id", table_name="flow_runner")
    op.drop_table("flow_runner")
    op.drop_index("ix_flow_execution_runner_id", table_name="flow_execution")
    op.drop_column("flow_execution", "runner_id")
    op.drop_column("flow", "runner_pool")
