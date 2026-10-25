"""Index purpose-tagged governance spend without scanning account traffic."""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261018_gov_spend_idx"
down_revision = "20261028_obs_verification"
branch_labels = None
depends_on = None

PREDICATE = "action_type = 'model_gateway' AND (meta_data ->> 'purpose') IN ('governance_session_digest', 'governance_review')"
INDEX = "ix_api_usage_governance_spend"


def upgrade() -> None:
    """Build without blocking gateway writes; repair an invalid prior attempt."""
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
                f"ON api_usage (account_id, (meta_data ->> 'funding'), timestamp) INCLUDE (id, estimated_cost) WHERE {PREDICATE}"
            )
        )


def downgrade() -> None:
    """Remove only the governance spend index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
