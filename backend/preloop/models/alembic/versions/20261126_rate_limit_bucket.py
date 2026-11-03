"""Shared fixed-window counters for the request rate limiter (EE #66)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261126_rate_limit_bucket"
down_revision = "20261125_runner_share_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the counter table. No existing data changes."""
    op.create_table(
        "rate_limit_bucket",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("key", name="uq_rate_limit_bucket_key"),
    )
    op.create_index(
        "ix_rate_limit_bucket_id", "rate_limit_bucket", ["id"], unique=False
    )
    op.create_index(
        "ix_rate_limit_bucket_expires_at",
        "rate_limit_bucket",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the counter table; counters are disposable."""
    op.drop_index("ix_rate_limit_bucket_expires_at", table_name="rate_limit_bucket")
    op.drop_index("ix_rate_limit_bucket_id", table_name="rate_limit_bucket")
    op.drop_table("rate_limit_bucket")
