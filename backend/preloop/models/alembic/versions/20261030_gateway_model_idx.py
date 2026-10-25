"""Cover the per-model gateway aggregate with a model-keyed index.

Revision ID: 20261030_gateway_model_idx
Revises: 20261029_audit_unsealed_idx

``GET /ai-models/overview`` took 15-30 s in production. Its aggregate,
``get_gateway_usage_by_model``, filters on ``ai_model_id IN (...)`` for the
models on the page. With that filter the planner preferred
``ix_api_usage_account_model_ts`` (account_id, ai_model_id, timestamp), whose
key matches it but which carries none of the summed columns: on prod that was
an Index Scan of 175k rows reading ~166k heap pages from disk (19.3 s).

``ix_api_usage_gateway_model_totals`` has the same key shape and INCLUDEs every
column the aggregate reads, with the predicate of
``exclude_replay_usage_condition`` (spelled with literals in the query, so
generic prepared plans can match it too), so that path becomes an index-only
scan. ``ix_api_usage_gateway_totals`` keeps serving the account-wide window.

Created CONCURRENTLY and IF NOT EXISTS, with an invalid leftover dropped
first, as in ``20261018_gov_spend_idx``. Run ``VACUUM (ANALYZE) api_usage``
after the build so the visibility map lets the planner cost it index-only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision: str = "20261030_gateway_model_idx"
down_revision: Union[str, None] = "20261029_audit_unsealed_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = "ix_api_usage_gateway_model_totals"
PREDICATE = (
    "action_type = 'model_gateway' AND "
    "((meta_data ->> 'purpose') IS NULL OR "
    "(meta_data ->> 'purpose') <> 'replay_validation')"
)
DEFINITION = (
    "ON api_usage (account_id, ai_model_id, timestamp) INCLUDE (id, "
    "status_code, prompt_tokens, completion_tokens, total_tokens, "
    "estimated_cost, cache_read_tokens, cache_creation_tokens, model_alias, "
    f"provider_name) WHERE {PREDICATE}"
)


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
            sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} {DEFINITION}")
        )


def downgrade() -> None:
    """Remove only the model-keyed gateway index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
