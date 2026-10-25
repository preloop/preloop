"""Cover customer reports without governance overhead or heap reads.

Revision ID: 20261101_customer_usage_idx
Revises: 20261031_activity_merge

Build new covering indexes concurrently. Retain the older replay-only indexes
for operational readers and safe rolling upgrades. This migration is restartable.
"""

from alembic import op
import sqlalchemy as sa

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261101_customer_usage_idx"
down_revision = "20261031_activity_merge"
branch_labels = None
depends_on = None

# Snapshot rather than importing mutable runtime vocabulary.
PREDICATE = (
    "action_type = 'model_gateway' AND "
    "((meta_data ->> 'purpose') IS NULL OR "
    "(meta_data ->> 'purpose') <> 'replay_validation') AND "
    "((meta_data ->> 'purpose') IS NULL OR "
    "left(meta_data ->> 'purpose', 11) <> 'governance_')"
)
COLUMNS = (
    "id, status_code, prompt_tokens, completion_tokens, total_tokens, "
    "estimated_cost, cache_read_tokens, cache_creation_tokens, model_alias, "
    "provider_name"
)
INDEXES = {
    "ix_api_usage_customer_totals": (
        f"ON api_usage (account_id, timestamp) INCLUDE ({COLUMNS}, ai_model_id, flow_id)"
    ),
    "ix_api_usage_customer_model_totals": (
        f"ON api_usage (account_id, ai_model_id, timestamp) INCLUDE ({COLUMNS})"
    ),
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
    """Remove the new indexes, preserving the pre-existing operational ones."""
    with concurrent_index_ddl(op.get_context()):
        for name in INDEXES:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
