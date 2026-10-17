"""Exclude completed history from the governance summary retry queue."""

from alembic import op
import sqlalchemy as sa

revision = "20261023_gov_retry_idx"
down_revision = "20261022_weekly_report_email"
branch_labels = None
depends_on = None

# Frozen independently of application imports for repeatable migrations.
PREDICATE = "(digest ->> 'summary_status') IN ('budget_limited', 'provider_failed', 'summarizing')"
INDEX = "ix_governance_session_digest_retry_queue"


def upgrade() -> None:
    """Create without blocking digest writes; repair an interrupted build."""
    with op.get_context().autocommit_block():
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
                "ON governance_session_digest "
                f"(account_id, window_end, runtime_session_id, segment_index, id) WHERE {PREDICATE}"
            )
        )


def downgrade() -> None:
    """Remove only the retry queue index."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
