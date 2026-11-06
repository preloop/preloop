"""Index the Spend page's session reads by account, newest first.

Revision ID: 20261203_session_keys_idx
Revises: 20261202_report_policy_merge

The Spend page ranks sessions by their newest request and counts the
distinct sessions of the window. Both reads filter customer gateway rows of
one account that belong to a session or a flow run. With no account-led
index carrying the session columns, the planner walked the global
``ix_api_usage_timestamp`` newest first and discarded every other tenant's
rows, or scanned the table for the count. This covering partial index makes
both an index-only range scan of the one account.

Created CONCURRENTLY and IF NOT EXISTS, with an invalid leftover dropped
first, as in ``20261101_customer_usage_idx``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision: str = "20261203_session_keys_idx"
down_revision: Union[str, None] = "20261202_report_policy_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = "ix_api_usage_customer_session_keys"
# Snapshot rather than importing mutable runtime vocabulary.
PREDICATE = (
    "action_type = 'model_gateway' AND "
    "((meta_data ->> 'purpose') IS NULL OR "
    "(meta_data ->> 'purpose') <> 'replay_validation') AND "
    "((meta_data ->> 'purpose') IS NULL OR "
    "left(meta_data ->> 'purpose', 11) <> 'governance_') AND "
    "(runtime_session_id IS NOT NULL OR flow_execution_id IS NOT NULL)"
)
DEFINITION = (
    "ON api_usage (account_id, timestamp DESC) "
    f"INCLUDE (runtime_session_id, flow_execution_id) WHERE {PREDICATE}"
)


def upgrade() -> None:
    """Build without blocking writes; repair an invalid prior attempt."""
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
            sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} {DEFINITION}")
        )


def downgrade() -> None:
    """Remove only the session key index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
