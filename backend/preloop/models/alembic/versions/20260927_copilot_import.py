"""Add GitHub Copilot usage import storage.

Revision ID: 20260927_copilot_import
Revises: 20260927_exec_log_type_ts
Create Date: 2026-09-27

Adds a per-user dimension and imported-usage markers to
``provider_billing_snapshot`` so Copilot premium-request rows for different
users do not collapse onto one dedup key, and a ``copilot_import_connection``
table holding the organization, tokens and operator-entered seat price.

The new snapshot columns are nullable and default to NULL, so existing
reconciliation rows keep their current dedup keys.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_copilot_import"
down_revision: Union[str, None] = "20260927_exec_log_type_ts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add snapshot columns and the Copilot import connection table."""
    op.add_column(
        "provider_billing_snapshot",
        sa.Column("user_login", sa.String(255), nullable=True),
    )
    op.add_column(
        "provider_billing_snapshot",
        sa.Column("usage_source", sa.String(16), nullable=True),
    )
    op.add_column(
        "provider_billing_snapshot",
        sa.Column("cost_basis", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "ck_provider_billing_snapshot_usage_source",
        "provider_billing_snapshot",
        "usage_source IS NULL OR usage_source IN ('imported')",
    )
    op.create_check_constraint(
        "ck_provider_billing_snapshot_cost_basis",
        "provider_billing_snapshot",
        "cost_basis IS NULL OR cost_basis IN ('estimated', 'reconciled')",
    )

    op.create_table(
        "copilot_import_connection",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization", sa.String(255), nullable=False),
        sa.Column("enterprise", sa.String(255), nullable=True),
        sa.Column(
            "secret_reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "enterprise_secret_reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("seat_price_monthly", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_day", sa.Date(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("per_user_billing_status", sa.String(16), nullable=True),
        sa.Column("per_user_billing_reason", sa.Text(), nullable=True),
        sa.Column("metrics_status", sa.String(16), nullable=True),
        sa.Column("metrics_reason", sa.Text(), nullable=True),
        sa.Column("last_warning", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("account_id", name="uq_copilot_import_connection_account"),
    )
    op.create_index(
        "ix_copilot_import_connection_account_id",
        "copilot_import_connection",
        ["account_id"],
    )


def downgrade() -> None:
    """Drop the Copilot import table and the snapshot columns."""
    op.drop_table("copilot_import_connection")
    op.execute("DELETE FROM provider_billing_snapshot WHERE usage_source = 'imported'")
    op.drop_constraint(
        "ck_provider_billing_snapshot_cost_basis",
        "provider_billing_snapshot",
        type_="check",
    )
    op.drop_constraint(
        "ck_provider_billing_snapshot_usage_source",
        "provider_billing_snapshot",
        type_="check",
    )
    op.drop_column("provider_billing_snapshot", "cost_basis")
    op.drop_column("provider_billing_snapshot", "usage_source")
    op.drop_column("provider_billing_snapshot", "user_login")
