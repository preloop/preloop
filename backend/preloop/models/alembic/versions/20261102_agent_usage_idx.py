"""Cover the agents list's per-principal usage totals with an index-only scan.

Revision ID: 20261102_agent_usage_idx
Revises: 20261101_customer_usage_idx

The agents registry sums each principal's gateway usage and reads its latest
request. ``ix_api_usage_account_principal_ts`` finds the rows but every one
was a heap fetch: 175k buffers for the founder account, 2.2 s on a cold cache
in prod (#1694). This index carries the summed columns so both LATERAL reads
stay inside the index. Built concurrently; restartable. Run
``VACUUM (ANALYZE) api_usage`` after the build so the visibility map covers
existing rows and the index-only scans skip the heap.
"""

from alembic import op
import sqlalchemy as sa

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261102_agent_usage_idx"
down_revision = "20261101_customer_usage_idx"
branch_labels = None
depends_on = None

NAME = "ix_api_usage_principal_totals"
DEFINITION = (
    "ON api_usage (account_id, runtime_principal_type, runtime_principal_id, "
    "timestamp DESC, id DESC) INCLUDE (status_code, prompt_tokens, "
    "completion_tokens, total_tokens, estimated_cost, cache_read_tokens, "
    "cache_creation_tokens, model_alias, provider_name) "
    "WHERE action_type = 'model_gateway'"
)


def upgrade() -> None:
    """Keep writes available and repair an index left invalid by interruption."""
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
    """Drop the covering index; the principal timestamp index remains."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}"))
