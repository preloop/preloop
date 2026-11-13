"""Let a user require a passkey to sign in (#2281).

Revision ID: 20261207_user_passkey_only
Revises: 20261206_session_away_since

``user.passkey_only`` is false for every existing row, so the setting is
inert until a user turns it on. While it is true, password, email-link,
chat and social sign-in are refused for that membership.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261207_user_passkey_only"
down_revision: Union[str, None] = "20261206_session_away_since"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the non-null flag, false for every existing user."""
    op.add_column(
        "user",
        sa.Column(
            "passkey_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Drop the flag."""
    op.drop_column("user", "passkey_only")
