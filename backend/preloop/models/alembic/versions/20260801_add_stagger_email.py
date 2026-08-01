"""Add stagger_email to notification_preferences.

Revision ID: 20260801_stagger_email
Revises: 20260730_webauthn_credentials
Create Date: 2026-08-01

Per-user toggle for staggered approval email (push first, email only if
still pending after 60s). Default True so existing rows opt in.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260801_stagger_email"
down_revision = "20260730_webauthn_credentials"
branch_labels = None
depends_on = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add stagger_email column with server default true."""
    op.add_column(
        "notification_preferences",
        sa.Column(
            "stagger_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment=(
                "When True and push is enabled, delay approval email until "
                "the request is still pending after the stagger window"
            ),
        ),
    )


def downgrade() -> None:
    """Remove stagger_email column."""
    op.drop_column("notification_preferences", "stagger_email")
