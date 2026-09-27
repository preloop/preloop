"""Index flow execution log rows by execution, log type, and time.

Revision ID: 20260927_exec_log_type_ts
Revises: 20260924_usage_principal_ts

The execution page reads the newest model calls of a run with
``WHERE execution_id = ? AND log_type IN (...) ORDER BY timestamp DESC
LIMIT n``. ``flow_execution_log`` only had single-column indexes on
``execution_id`` and ``timestamp``, so for a run made mostly of agent log
lines the planner walked every row of the run to find the calls. This
composite index serves that read directly and any other ``log_type`` filter
on one execution.

The index is built inside the migration transaction, like the other index
migrations in this history. That takes a SHARE lock on ``flow_execution_log``
and pauses log inserts for the duration of the build. A deployment with a very
large ``flow_execution_log`` should build it concurrently instead
(``postgresql_concurrently=True`` inside ``op.get_context().autocommit_block()``,
with the matching concurrent drop).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260927_exec_log_type_ts"
down_revision: Union[str, None] = "20260924_usage_principal_ts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_INDEX_NAME = "ix_flow_execution_log_execution_type_ts"


def upgrade() -> None:
    """Index log rows by execution, log type, and timestamp."""
    op.create_index(
        _INDEX_NAME,
        "flow_execution_log",
        ["execution_id", "log_type", "timestamp"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the execution, log type, and timestamp index."""
    op.drop_index(_INDEX_NAME, table_name="flow_execution_log")
