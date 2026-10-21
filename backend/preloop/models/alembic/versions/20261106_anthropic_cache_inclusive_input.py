"""Store existing Anthropic gateway input cache inclusive (#1955).

Revision ID: 20261106_anthropic_cache_input
Revises: 20261105_fleet_work_tools

The gateway used to store a raw Anthropic ``input_tokens`` (which leaves the
prompt-cache read and write out) as ``prompt_tokens``, while every other
provider's input, and the uncached remainder ``input - read - write``, is cache
inclusive. New rows are written cache inclusive; this backfill brings the
existing ones to the same shape so aggregates need no read-time guard.

A row qualifies only when its raw provider usage in
``meta_data.usage_details`` is the Anthropic shape (top-level
``cache_read_input_tokens`` / ``cache_creation_input_tokens``, no
``prompt_tokens``) and ``prompt_tokens`` still equals that raw
``input_tokens``. The added count is recorded in
``meta_data.input_cache_backfill`` so the downgrade reverts exactly this
change and a second run is a no-op. ``estimated_cost`` is untouched.
Catalog-priced rows were priced from the raw usage, which already separates
the cache, so their cost is right. Rows priced by a configured price or the
DeepSeek native tariff subtracted the cache from the exclusive input and so
left out the uncached part; they keep that undercount (the uncached part of
an Anthropic request is usually small next to the cache). New rows are priced
from cache-inclusive input and are right on every path.

Rows that pods on the previous release write after this revision commits are
converted by ``crud_api_usage.repair_anthropic_cache_input``, which the API
runs once a delay after startup over the last day of rows.

Deployment cost: one scan of api_usage, rewriting only matching rows, under a
ROW EXCLUSIVE table lock (reads and writes continue) and row locks on changed
records until commit. Imported usage is excluded because its metadata is
untrusted.
"""

from alembic import op

revision = "20261106_anthropic_cache_input"
down_revision = "20261105_fleet_work_tools"
branch_labels = None
depends_on = None

MARKER = "input_cache_backfill"


def upgrade() -> None:
    """Add the cache read and write to raw Anthropic input totals."""
    op.execute(f"""
        WITH candidate AS (
            SELECT id,
                   coalesce((meta_data -> 'usage_details'
                             ->> 'cache_read_input_tokens')::bigint, 0)
                 + coalesce((meta_data -> 'usage_details'
                             ->> 'cache_creation_input_tokens')::bigint, 0)
                   AS cache_tokens
            FROM api_usage
            WHERE action_type = 'model_gateway'
              AND usage_source IS DISTINCT FROM 'imported'
              AND NOT (meta_data ? '{MARKER}')
              AND jsonb_typeof(meta_data -> 'usage_details') = 'object'
              AND NOT (meta_data -> 'usage_details' ? 'prompt_tokens')
              AND (meta_data -> 'usage_details' ->> 'input_tokens') ~ '^[0-9]+$'
              AND coalesce(meta_data -> 'usage_details'
                           ->> 'cache_read_input_tokens', '0') ~ '^[0-9]+$'
              AND coalesce(meta_data -> 'usage_details'
                           ->> 'cache_creation_input_tokens', '0') ~ '^[0-9]+$'
              AND (meta_data -> 'usage_details' ? 'cache_read_input_tokens'
                   OR meta_data -> 'usage_details'
                      ? 'cache_creation_input_tokens')
              AND prompt_tokens
                  = (meta_data -> 'usage_details' ->> 'input_tokens')::bigint
        )
        UPDATE api_usage AS u
        SET prompt_tokens = u.prompt_tokens + c.cache_tokens,
            total_tokens = CASE
                WHEN u.total_tokens IS NULL THEN NULL
                ELSE u.total_tokens + c.cache_tokens
            END,
            meta_data = jsonb_set(u.meta_data, '{{{MARKER}}}',
                                  to_jsonb(c.cache_tokens))
        FROM candidate AS c
        WHERE u.id = c.id
          AND c.cache_tokens > 0
          AND NOT (u.meta_data ? '{MARKER}')
          AND u.prompt_tokens
              = (u.meta_data -> 'usage_details' ->> 'input_tokens')::bigint
    """)


def downgrade() -> None:
    """Subtract exactly what the upgrade added, then drop the marker."""
    op.execute(f"""
        UPDATE api_usage
        SET prompt_tokens = prompt_tokens - (meta_data ->> '{MARKER}')::bigint,
            total_tokens = CASE
                WHEN total_tokens IS NULL THEN NULL
                ELSE total_tokens - (meta_data ->> '{MARKER}')::bigint
            END,
            meta_data = meta_data - '{MARKER}'
        WHERE meta_data ? '{MARKER}'
    """)
