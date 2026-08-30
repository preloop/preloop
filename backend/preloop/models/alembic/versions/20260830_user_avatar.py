"""Add avatar_url and avatar_source columns to user table.

Revision ID: 20260830_user_avatar
Revises: 20260822_alias_collision_audit
Create Date: 2026-08-30

Adds nullable columns for user profile images. avatar_url stores either an
external provider URL (SSO) or a base64 data URI (manual upload).
avatar_source records provenance ("sso" or "manual") so the precedence rule
(manual > sso > default) can be enforced on updates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260830_user_avatar"
down_revision = "20260822_alias_collision_audit"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add avatar columns to user table."""
    op.add_column(
        "user",
        sa.Column(
            "avatar_url",
            sa.Text(),
            nullable=True,
            comment="Profile image URL or base64 data URI",
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "avatar_source",
            sa.String(20),
            nullable=True,
            comment="Avatar provenance: 'sso' or 'manual'",
        ),
    )


def downgrade() -> None:
    """Remove avatar columns from user table."""
    op.drop_column("user", "avatar_source")
    op.drop_column("user", "avatar_url")
