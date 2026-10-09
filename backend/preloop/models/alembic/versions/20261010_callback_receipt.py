"""Durable content-free callback verdict receipts.

Revision ID: 20261010_callback_receipt
Revises: 20261009_discovery_observation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261010_callback_receipt"
down_revision = "20261009_discovery_observation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create receipts without transcript or credential columns."""
    op.create_table(
        "callback_receipt",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "integration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delivery_digest", sa.String(64), nullable=False),
        sa.Column("body_digest", sa.String(64), nullable=False),
        sa.Column("verdict", postgresql.JSONB(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "integration_id",
            "delivery_digest",
            name="uq_callback_receipt_delivery",
        ),
    )
    op.create_index("ix_callback_receipt_id", "callback_receipt", ["id"])
    op.create_index("ix_callback_receipt_expiry", "callback_receipt", ["expires_at"])
    op.create_index(
        "ix_callback_receipt_account_integration",
        "callback_receipt",
        ["account_id", "integration_id", "created_at"],
    )

    op.create_table(
        "callback_key_binding",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "integration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("signing_key_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("digest_epoch", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_callback_key_binding_id", "callback_key_binding", ["id"])
    op.create_index(
        "ix_callback_key_binding_integration_id",
        "callback_key_binding",
        ["integration_id"],
    )


def downgrade() -> None:
    """Remove callback receipts."""
    op.drop_table("callback_key_binding")
    op.drop_table("callback_receipt")
