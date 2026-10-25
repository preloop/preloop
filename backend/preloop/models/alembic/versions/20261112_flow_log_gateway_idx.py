"""Index flow execution log model calls for the compaction walk.

Revision ID: 20261112_flow_log_gateway_idx
Revises: 20261111_credential_columns

The gateway activity maintenance pass walks ``model_gateway_call`` rows of
``flow_execution_log`` across all executions, oldest first on
``(timestamp, id)``. No index led on ``log_type``, so each page had to read
past the agent log lines that make up most of the table. This partial index
holds only model call rows, in walk order.

Built ``CONCURRENTLY`` so log inserts are not blocked on a large table; an
invalid index left by an interrupted build is dropped and rebuilt.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261112_flow_log_gateway_idx"
down_revision: Union[str, None] = "20261111_credential_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

INDEX = "ix_flow_execution_log_gateway_call_ts"
DEFINITION = (
    "ON flow_execution_log (timestamp, id) WHERE log_type = 'model_gateway_call'"
)


def upgrade() -> None:
    """Build the partial index without blocking writes."""
    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": INDEX},
            )
            .scalar()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} {DEFINITION}")


def downgrade() -> None:
    """Drop the partial index without blocking writes."""
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
