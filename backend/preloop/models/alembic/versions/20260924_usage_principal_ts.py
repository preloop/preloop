"""Index per-user model-gateway usage by principal and time.

Revision ID: 20260924_usage_principal_ts
Revises: 20260924_browser_step_idx

Account usage summaries filter ``runtime_principal_id`` without
``runtime_principal_type``. ``ix_api_usage_account_principal_ts`` leads with
the type column, so the planner cannot use it for that filter and scans the
account's raw rows. This partial index is the measured access path for the
per-user window.

The index is built inside the migration transaction, like every other index
migration in this history. That takes a SHARE lock on ``api_usage`` and pauses
gateway usage inserts for the duration of the build, which is acceptable at
current ``api_usage`` sizes because the partial predicate limits the build to
``model_gateway`` rows. A deployment with tens of millions of rows should build
it concurrently instead (``postgresql_concurrently=True`` inside
``op.get_context().autocommit_block()``, with the matching concurrent drop).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_usage_principal_ts"
down_revision: Union[str, None] = "20260924_browser_step_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_INDEX_NAME = "ix_api_usage_account_principal_id_ts"


def upgrade() -> None:
    """Index model-gateway rows by account, principal, and timestamp."""
    op.create_index(
        _INDEX_NAME,
        "api_usage",
        ["account_id", "runtime_principal_id", "timestamp"],
        unique=False,
        postgresql_ops={"timestamp": "DESC"},
        postgresql_where=sa.text("action_type = 'model_gateway'"),
    )


def downgrade() -> None:
    """Drop the per-user model-gateway principal index."""
    op.drop_index(_INDEX_NAME, table_name="api_usage")
