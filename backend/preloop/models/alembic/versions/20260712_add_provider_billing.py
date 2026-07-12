"""Add provider billing connection and snapshot tables.

Revision ID: 20260712_provider_billing
Revises: 20260712_tracker_secret
Create Date: 2026-07-12

Stores per-account provider admin-API connections and the billing/usage
actuals fetched from them, so estimated gateway spend can be reconciled
against provider-reported cost.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260712_provider_billing"
down_revision: Union[str, None] = "20260712_tracker_secret"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create provider billing tables."""
    op.create_table(
        "provider_billing_connection",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column(
            "secret_reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id", "provider", name="uq_provider_billing_connection"
        ),
    )
    op.create_index(
        "ix_provider_billing_connection_account_id",
        "provider_billing_connection",
        ["account_id"],
    )

    op.create_table(
        "provider_billing_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("granularity", sa.String(8), nullable=False, server_default="1d"),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("line_item", sa.String(255), nullable=True),
        sa.Column("provider_api_key_id", sa.String(255), nullable=True),
        sa.Column("project_or_workspace_id", sa.String(255), nullable=True),
        sa.Column("service_tier", sa.String(64), nullable=True),
        sa.Column("cost_amount", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("uncached_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_creation_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id", "dedup_key", name="uq_provider_billing_snapshot_dedup"
        ),
    )
    op.create_index(
        "ix_provider_billing_snapshot_account_id",
        "provider_billing_snapshot",
        ["account_id"],
    )
    op.create_index(
        "ix_provider_billing_snapshot_window",
        "provider_billing_snapshot",
        ["account_id", "provider", "bucket_start"],
    )
    op.create_index(
        "ix_provider_billing_snapshot_bucket_start",
        "provider_billing_snapshot",
        ["bucket_start"],
    )


def downgrade() -> None:
    """Drop provider billing tables."""
    op.drop_table("provider_billing_snapshot")
    op.drop_table("provider_billing_connection")
