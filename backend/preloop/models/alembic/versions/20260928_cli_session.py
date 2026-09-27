"""Add cli_session so each CLI JWT login can be revoked on its own.

One row per ``preloop auth login``. Access and refresh tokens carry the row
id as ``sid``; the refresh token also carries a ``jti`` that must equal
``refresh_jti``. Setting ``revoked_at`` rejects both tokens (#839).

Revision ID: 20260928_cli_session
Revises: 20260927_policy_notice_hit
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260928_cli_session"
down_revision: Union[str, None] = "20260927_policy_notice_hit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the cli_session table."""
    op.create_table(
        "cli_session",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "refresh_jti",
            sa.String(64),
            nullable=False,
            comment="jti of the current refresh token; older ones are rejected",
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("hostname", sa.String(255), nullable=True),
    )
    op.create_index("ix_cli_session_id", "cli_session", ["id"])
    op.create_index("ix_cli_session_user_id", "cli_session", ["user_id"])


def downgrade() -> None:
    """Drop the cli_session table."""
    op.drop_index("ix_cli_session_user_id", table_name="cli_session")
    op.drop_index("ix_cli_session_id", table_name="cli_session")
    op.drop_table("cli_session")
