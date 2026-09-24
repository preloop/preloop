"""Index browser-step idempotency lookups on runtime session activity.

Adapters retry a flush after a dropped response. The lookup is
``(runtime_session_id, metadata.source, metadata.source_step_id)`` for
rows whose ``activity_type`` is ``browser_step``. The index is not unique:
the writer returns the existing row when the key matches, and a unique
constraint would turn that retry into an integrity error.

Revision ID: 20260924_browser_step_idx
Revises: 20260921_auth_generation
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_browser_step_idx"
down_revision: Union[str, None] = "20260921_auth_generation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_INDEX_NAME = "ix_runtime_session_activity_browser_step_idem"


def upgrade() -> None:
    """Index browser-step source and source_step_id within a session."""
    op.create_index(
        _INDEX_NAME,
        "runtime_session_activity",
        [
            "runtime_session_id",
            sa.text("(metadata ->> 'source')"),
            sa.text("(metadata ->> 'source_step_id')"),
        ],
        unique=False,
        postgresql_where=sa.text("activity_type = 'browser_step'"),
    )


def downgrade() -> None:
    """Drop the browser-step idempotency index."""
    op.drop_index(_INDEX_NAME, table_name="runtime_session_activity")
