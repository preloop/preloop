"""Index parse-failed summary retries without scanning completed history.

Keep the original queue index for rolling upgrades. Build the new partial
index concurrently and repair interrupted builds on retry.
"""

from alembic import op
import sqlalchemy as sa

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261103_gov_parse_retry"
down_revision = "20261102_restore_usage_action"
branch_labels = None
depends_on = None

# Frozen independently of application imports for repeatable migrations.
PREDICATE = "(digest ->> 'summary_status') = 'parse_failed'"
INDEX = "ix_governance_session_digest_parse_retry"
INDEXES = {
    INDEX: "ON governance_session_digest "
    "(account_id, window_end, runtime_session_id, segment_index, id)"
}


def upgrade() -> None:
    """Keep writes available and repair indexes left invalid by interruption."""
    with concurrent_index_ddl(op.get_context()):
        for name, definition in INDEXES.items():
            valid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                        "ON c.oid = i.indexrelid WHERE c.relname = :name "
                        "AND c.relnamespace = to_regnamespace(current_schema())"
                    ),
                    {"name": name},
                )
                .scalar()
            )
            if valid is False:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition} WHERE {PREDICATE}"
                )
            )


def downgrade() -> None:
    """Remove only the parse-failure retry index."""
    with concurrent_index_ddl(op.get_context()):
        for name in INDEXES:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
