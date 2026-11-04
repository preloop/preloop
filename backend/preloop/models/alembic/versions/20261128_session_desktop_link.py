"""Let a runtime session name the desktop endpoint it runs next to (#1568).

Revision ID: 20261128_session_desktop_link
Revises: 20261127_rate_limit_bucket

Adds one nullable column to ``runtime_session``. Adapters send the value of
``PRELOOP_DESKTOP_ENDPOINT_ID``; the desktop link only matches endpoints in
the session's own account, so the column needs no foreign key. Existing rows
stay NULL and keep the current linkage rules.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261128_session_desktop_link"
down_revision = "20261127_rate_limit_bucket"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable column."""
    op.add_column(
        "runtime_session",
        sa.Column("desktop_endpoint_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("runtime_session", "desktop_endpoint_id")
