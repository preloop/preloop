"""Add enrollment hostname and identity derivation to managed_agent.

Revision ID: 20260801_agent_identity_v2
Revises: 20260731_usage_imported
Create Date: 2026-08-01

Additive nullable columns used by CLI v2 principal-id derivation. No backfill;
values arrive on the next CLI contact per install.

Re-parented onto ``20260731_usage_imported`` after #137/#140 merged (file
``20260731_usage_source_imported.py``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260801_agent_identity_v2"
down_revision = "20260731_usage_imported"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add nullable identity metadata columns on managed_agent."""
    op.add_column(
        "managed_agent",
        sa.Column("enrollment_hostname", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "managed_agent",
        sa.Column("identity_derivation", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    """Drop identity metadata columns."""
    op.drop_column("managed_agent", "identity_derivation")
    op.drop_column("managed_agent", "enrollment_hostname")
