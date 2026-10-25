"""Cover successful issue and PR tools in the Fleet Map work window.

Revision ID: 20261105_fleet_work_tools
Revises: 20261104_session_write_perms
"""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261105_fleet_work_tools"
down_revision = "20261104_session_write_perms"
branch_labels = None
depends_on = None

INDEX = "ix_runtime_session_activity_work_tools"
# Frozen migration predicate, matching WORK_ACTIVITY_TOOLS at introduction.
TOOLS = (
    "add_comment",
    "add_comment_to_pending_review",
    "approve_merge_request",
    "create_merge_request",
    "create_merge_request_note",
    "create_pull_request",
    "create_pull_request_review",
    "merge_merge_request",
    "merge_pull_request",
    "pull_request_review_write",
    "submit_pending_pull_request_review",
    "update_pull_request",
    "create_issue",
    "update_issue",
    "update_merge_request",
)


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
        tools = ", ".join(f"'{name}'" for name in TOOLS)
        op.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
                "ON runtime_session_activity (account_id, timestamp DESC) "
                "INCLUDE (tool_name, flow_execution_id, runtime_session_id) "
                "WHERE activity_type = 'tool_call' AND status = 'succeeded' "
                f"AND tool_name IN ({tools})"
            )
        )


def downgrade() -> None:
    """Remove only the work-history tool index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
