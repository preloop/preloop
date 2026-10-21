"""Bound durable Agent Control result lookups by account and command ID."""

from contextlib import contextmanager
from collections.abc import Iterator

import sqlalchemy as sa
from alembic import op


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


@contextmanager
def _index_ddl() -> Iterator[None]:
    # Extracting command IDs can read toasted historical payloads. Keep this
    # build's finite scan budget longer than the general concurrent-index
    # helper, without changing other migrations or the session defaults.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        previous_lock, previous_statement = bind.execute(
            sa.text(
                "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
            )
        ).one()
        settings = sa.text(
            "SELECT set_config('lock_timeout', :lock, false), set_config('statement_timeout', :statement, false)"
        )
        try:
            bind.execute(settings, {"lock": "60s", "statement": "30min"})
            yield
        finally:
            bind.execute(
                settings, {"lock": previous_lock, "statement": previous_statement}
            )


def upgrade() -> None:
    """Build without blocking writes, including after an interrupted attempt."""
    with _index_ddl():
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
    with _index_ddl():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
