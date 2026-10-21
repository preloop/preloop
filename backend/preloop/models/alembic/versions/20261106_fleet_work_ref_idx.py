"""Index classified work tool calls for the Fleet Map.

Revision ID: 20261106_fleet_work_ref_idx
Revises: 20261105_fleet_work_tools
"""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261106_fleet_work_ref_idx"
down_revision = "20261105_fleet_work_tools"
branch_labels = None
depends_on = None

INDEX = "ix_runtime_session_activity_work_ref"


def upgrade() -> None:
    """Build concurrently and repair an invalid interrupted build."""
    with concurrent_index_ddl(op.get_context()):
        valid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                    "ON c.oid = i.indexrelid WHERE c.relname = :name"
                ),
                {"name": INDEX},
            )
            .scalar()
        )
        if valid is False:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
        op.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
                "ON runtime_session_activity (account_id, timestamp DESC) "
                "INCLUDE (tool_name, flow_execution_id, runtime_session_id) "
                "WHERE activity_type = 'tool_call' AND metadata ? 'work_ref' "
                "AND status IN ('succeeded', 'allowed', 'approved')"
            )
        )


def downgrade() -> None:
    """Remove only the classified work-call index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
