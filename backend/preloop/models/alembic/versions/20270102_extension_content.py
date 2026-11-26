"""Store redacted extension content for review.

Revision ID: 20270102_extension_content
Revises: 20270101_extension_details

Adds ``extension_content``: one encrypted document per account and content
hash, uploaded by a collector so an approver can read what an extension
contains. No rows are created here. Accounts that reported inventory before
this revision keep exactly what they had: content only arrives from a
collector version that prints the content notice on the device.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20270102_extension_content"
down_revision: Union[str, None] = "20270101_extension_details"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the content table, scoped to and deleted with its account."""
    op.create_table(
        "extension_content",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("extension_kind", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(80), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("stored_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("entry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stored_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("withheld_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("redaction_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("withheld_reason", sa.String(32)),
        sa.Column("verification", sa.String(16), server_default="none", nullable=False),
        sa.Column(
            "redacted", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("redaction_rules", sa.String(64)),
        sa.Column("collector_version", sa.String(64)),
        sa.Column("uploaded_fingerprint", sa.String(128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_reported_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id",
            "extension_kind",
            "content_hash",
            name="uq_extension_content_account_hash",
        ),
        sa.CheckConstraint(
            "extension_kind IN ('plugin','skill','hook','command')",
            name="ck_extension_content_kind",
        ),
        sa.CheckConstraint(
            "verification IN ('verified','partial','unverified','none')",
            name="ck_extension_content_verification",
        ),
    )
    op.create_index("ix_extension_content_id", "extension_content", ["id"])
    op.create_index(
        "ix_extension_content_account_id", "extension_content", ["account_id"]
    )
    op.create_index(
        "ix_extension_content_last_reported", "extension_content", ["last_reported_at"]
    )


def downgrade() -> None:
    """Drop the table and every stored content with it."""
    op.drop_table("extension_content")
