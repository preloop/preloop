"""Index account-scoped refusal reason filters without rewriting audit rows."""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261220_denial_reason_idx"
down_revision = "20261219_ext_grant_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Build the partial lookup index without blocking audit inserts."""
    with concurrent_index_ddl(op.get_context()):
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'ix_audit_log_account_reason_code' "
                    "AND n.nspname = current_schema() AND NOT i.indisvalid"
                )
            )
            .scalar()
        )
        if invalid:
            op.execute("DROP INDEX CONCURRENTLY ix_audit_log_account_reason_code")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_log_account_reason_code "
            "ON audit_log (account_id, (details ->> 'reason_code')) "
            "WHERE (details ->> 'reason_code') IS NOT NULL"
        )


def downgrade() -> None:
    """Remove only the refusal lookup index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_audit_log_account_reason_code")
