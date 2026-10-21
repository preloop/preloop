"""Index capture-incomplete governance summaries for retry.

Keep the existing provider and parse retry indexes for rolling upgrades.
"""

from alembic import op
import sqlalchemy as sa

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261107_gov_capture_retry"
down_revision = "20261106_fleet_work_ref_idx"
branch_labels = None
depends_on = None

# Frozen independently of application imports for repeatable migrations.
PREDICATE = "(digest ->> 'summary_status') = 'capture_incomplete'"
INDEX = "ix_governance_session_digest_capture_retry"
DEFINITION = (
    "ON governance_session_digest "
    "(account_id, window_end, runtime_session_id, segment_index, id)"
)


def upgrade() -> None:
    """Build the partial retry index without blocking digest writes."""
    with concurrent_index_ddl(op.get_context()):
        valid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                    "ON c.oid = i.indexrelid WHERE c.relname = :name "
                    "AND c.relnamespace = to_regnamespace(current_schema())"
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
                f"{DEFINITION} WHERE {PREDICATE}"
            )
        )


def downgrade() -> None:
    """Remove only the capture retry index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
