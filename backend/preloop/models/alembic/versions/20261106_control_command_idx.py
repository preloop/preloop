"""Bound durable Agent Control result lookups by account and command ID."""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261106_control_command_idx"
down_revision = "20261105_fleet_work_tools"
branch_labels = None
depends_on = None

INDEX = "ix_rsa_account_command_lookup"
DEFINITION = (
    "ON runtime_session_activity "
    "(account_id, ((metadata ->> 'command_id')), timestamp DESC) "
    "WHERE (metadata ->> 'command_id') IS NOT NULL"
)


def upgrade() -> None:
    """Build without blocking writes, including after an interrupted attempt."""
    with concurrent_index_ddl(op.get_context()):
        valid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                    "ON c.oid = i.indexrelid WHERE c.relname = :name "
                    "AND c.relnamespace = current_schema()::regnamespace"
                ),
                {"name": INDEX},
            )
            .scalar()
        )
        if valid is False:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
        op.execute(
            sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} {DEFINITION}")
        )


def downgrade() -> None:
    """Remove only the command correlation lookup index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
