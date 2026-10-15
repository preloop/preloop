"""Index real session traffic for Stage A candidate probes."""

from alembic import op
import sqlalchemy as sa

revision = "20261017_gov_candidate_idx"
down_revision = "20261014_model_detail_idx"
branch_labels = None
depends_on = None

PREDICATE = "action_type = 'model_gateway' AND ((meta_data ->> 'purpose') IS NULL OR (meta_data ->> 'purpose') NOT IN ('governance_review', 'governance_session_digest', 'replay_validation', 'session_embedding', 'session_optimization', 'session_title'))"
INDEX = "ix_api_usage_governance_candidate"


def upgrade() -> None:
    """Build without blocking gateway writes; repair an invalid prior attempt."""
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
                f"ON api_usage (account_id, runtime_session_id, timestamp) WHERE {PREDICATE}"
            )
        )


def downgrade() -> None:
    """Remove only the Stage A candidate index."""
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
