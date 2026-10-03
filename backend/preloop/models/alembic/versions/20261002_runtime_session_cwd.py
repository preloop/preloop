"""Record the working directory a hook reported for a runtime session.

Revision ID: 20261002_runtime_session_cwd
Revises: 20261001_artifact_kinds_labels
Create Date: 2026-10-02

Additive only. ``runtime_session.cwd`` holds the last working directory a
governed agent's permission hook reported for the session, so the session list
can label rows whose title, summary and reference are still empty (#1148).
NULL for every existing row and for agents whose hook sends no ``cwd``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20261002_runtime_session_cwd"
down_revision = "20261001_artifact_kinds_labels"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable cwd column."""
    op.add_column(
        "runtime_session",
        sa.Column("cwd", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    """Drop the cwd column."""
    op.drop_column("runtime_session", "cwd")
