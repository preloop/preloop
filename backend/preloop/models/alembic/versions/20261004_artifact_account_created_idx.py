"""Index session artifacts by account and creation time.

Revision ID: 20261004_artifact_created_idx
Revises: 20261003_issue_extid_unique

The account-wide artifact search (#1086) pages by ``(created_at, id)``
newest first, and its facet counts read the newest 10000 matching rows.
``(account_id, kind, created_at)`` serves that order only when ``kind`` is
fixed; without a kind filter every artifact of the account was sorted per
request. ``(account_id, created_at DESC, id DESC)`` serves the unfiltered
page and the facet cap with an index scan.

Created CONCURRENTLY (no write lock on ``runtime_session_artifact``) and
IF NOT EXISTS, with a leftover invalid index dropped first, as in
``20261003_resume_root_idx``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261004_artifact_created_idx"
down_revision: Union[str, None] = "20261003_issue_extid_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

INDEX = "ix_runtime_session_artifact_account_created"


def upgrade() -> None:
    """Create the index without blocking writes; rebuild an invalid leftover."""
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
            "ON runtime_session_artifact (account_id, created_at DESC, id DESC)"
        )


def downgrade() -> None:
    """Drop the index without blocking writes."""
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
