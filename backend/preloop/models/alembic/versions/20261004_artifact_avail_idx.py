"""Index available artifacts for the session-list has_artifacts filter.

Revision ID: 20261004_artifact_avail_idx
Revises: 20261003_discovery_candidates

``has_artifacts=any`` selects ``runtime_session_id`` for one account where
``availability = 'available'``. A kind filter adds ``kind =``. Neither
predicate was covered: ``(account_id)`` still fetched every artifact row,
and ``(account_id, kind, created_at)`` cannot serve a list that does not
order by ``created_at``.

This partial index holds only available rows, with ``kind`` as a key so a
kind filter does not read the account's other kinds, and
``runtime_session_id`` so the distinct session-id subquery is index-only.

Created CONCURRENTLY (no write lock on ``runtime_session_artifact``) and
IF NOT EXISTS, with a leftover invalid index dropped first, as in
``20261004_artifact_created_idx``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261004_artifact_avail_idx"
down_revision: Union[str, None] = "20261003_discovery_candidates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

INDEX = "ix_runtime_session_artifact_available_holders"
# Must stay textually identical to the model index and to the literal
# ``availability = 'available'`` in sessions_with_available_artifacts, or
# the planner will not match the partial predicate.
PREDICATE = "availability = 'available'"


def upgrade() -> None:
    """Create the partial index without blocking writes; rebuild an invalid leftover."""
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
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON runtime_session_artifact (account_id, kind, runtime_session_id) "
            f"WHERE {PREDICATE}"
        )


def downgrade() -> None:
    """Drop the index without blocking writes."""
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
