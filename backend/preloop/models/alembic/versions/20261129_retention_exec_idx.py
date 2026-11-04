"""Bound account-scoped execution retention lookups by flow and creation date.

Revision ID: 20261129_retention_exec_idx
Revises: 20261128_session_desktop_link
"""

from alembic import op
import sqlalchemy as sa

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261129_retention_exec_idx"
down_revision = "20261128_session_desktop_link"
branch_labels = None
depends_on = None

NAME = "ix_flow_execution_flow_created"
DEFINITION = "ON flow_execution (flow_id, created_at)"


def upgrade() -> None:
    """Build the retention index concurrently and repair an invalid build."""
    with concurrent_index_ddl(op.get_context()):
        valid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                    "ON c.oid = i.indexrelid WHERE c.relname = :name "
                    "AND c.relnamespace = to_regnamespace(current_schema())"
                ),
                {"name": NAME},
            )
            .scalar()
        )
        if valid is False:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}"))
        op.execute(
            sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME} {DEFINITION}")
        )


def downgrade() -> None:
    """Drop the retention index without blocking writes."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}"))
