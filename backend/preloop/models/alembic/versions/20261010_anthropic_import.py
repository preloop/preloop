"""Add the Anthropic usage import connection and actor mappings (#1413).

Revision ID: 20261010_anthropic_import
Revises: 20261009_resource_sharing_intent
Create Date: 2026-10-10

Imported rows reuse ``provider_billing_snapshot`` (``usage_source``,
``cost_basis`` and ``user_login`` already exist), so only the connection and
mapping tables are new.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261010_anthropic_import"
down_revision: Union[str, None] = "20261009_resource_sharing_intent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create ``anthropic_import_connection`` and ``anthropic_user_mapping``."""
    op.create_table(
        "anthropic_import_connection",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "secret_reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_hint", sa.String(4), nullable=True),
        sa.Column(
            "gateway_key_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_day", sa.Date(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_warning", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id", name="uq_anthropic_import_connection_account"
        ),
    )
    op.create_index(
        "ix_anthropic_import_connection_account_id",
        "anthropic_import_connection",
        ["account_id"],
    )
    op.create_table(
        "anthropic_user_mapping",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("anthropic_import_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id", "actor", name="uq_anthropic_user_mapping_actor"
        ),
    )
    op.create_index(
        "ix_anthropic_user_mapping_account_user",
        "anthropic_user_mapping",
        ["account_id", "user_id"],
    )


def downgrade() -> None:
    """Drop both tables and the imported Anthropic snapshot rows."""
    op.drop_table("anthropic_user_mapping")
    op.drop_table("anthropic_import_connection")
    op.execute(
        "DELETE FROM provider_billing_snapshot "
        "WHERE usage_source = 'imported' "
        "AND provider IN ('anthropic_cc', 'anthropic_usage')"
    )
