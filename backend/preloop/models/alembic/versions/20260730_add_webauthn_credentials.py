"""Add webauthn_credential table for passkey support.

Revision ID: 20260730_webauthn_credentials
Revises: 20260719_optimization_job
Create Date: 2026-07-30

Adds ``webauthn_credential``: one row per registered passkey authenticator.
Scope is single-device passkeys with discoverable credentials; no admin
management surface.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "20260730_webauthn_credentials"
down_revision = "20260719_optimization_job"
branch_labels = None
depends_on = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the webauthn_credential table."""
    op.create_table(
        "webauthn_credential",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("transports", sa.Text(), nullable=True),
        sa.Column(
            "name",
            sa.String(length=100),
            nullable=False,
            server_default="Passkey",
        ),
        sa.Column("aaguid", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_webauthn_credential_user_id",
        "webauthn_credential",
        ["user_id"],
    )
    op.create_index(
        "ix_webauthn_credential_credential_id",
        "webauthn_credential",
        ["credential_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the webauthn_credential table."""
    op.drop_index(
        "ix_webauthn_credential_credential_id", table_name="webauthn_credential"
    )
    op.drop_index("ix_webauthn_credential_user_id", table_name="webauthn_credential")
    op.drop_table("webauthn_credential")
