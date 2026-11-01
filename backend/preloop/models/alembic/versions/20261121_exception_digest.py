"""Durable factual digest snapshots, material state and synthesis reservation.

Revision ID: 20261121_exception_digest
Revises: 20261119_credentials_at_rest
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261121_exception_digest"
down_revision = "20261119_credentials_at_rest"
branch_labels = None
depends_on = None


def base_columns() -> list[sa.Column]:
    """Use the shared Base identity and timestamp contract."""
    return [
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def reference(name: str, target: str) -> sa.Column:
    """Keep recipient/account deletion semantics consistent."""
    return sa.Column(
        name, sa.UUID(), sa.ForeignKey(target, ondelete="CASCADE"), nullable=False
    )


def upgrade() -> None:
    """Add delivery state without changing existing email or analysis settings."""
    op.create_table(
        "exception_digest_week",
        *base_columns(),
        reference("account_id", "account.id"),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "synthesis_reserved_usd",
            sa.Numeric(16, 8),
            server_default="0",
            nullable=False,
        ),
        sa.Column("synthesis_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("synthesis_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("synthesis_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "account_id", "window_end", name="uq_exception_digest_week"
        ),
    )
    op.create_table(
        "exception_digest_delivery",
        *base_columns(),
        reference("week_id", "exception_digest_week.id"),
        reference("user_id", "user.id"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("sections", JSONB(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("week_id", "user_id", name="uq_exception_digest_recipient"),
    )
    op.create_table(
        "exception_digest_state",
        *base_columns(),
        reference("account_id", "account.id"),
        reference("user_id", "user.id"),
        sa.Column("exception_key", sa.String(64), nullable=False),
        sa.Column("source_scope", sa.String(80), nullable=True),
        sa.Column("delivered_fingerprint", sa.String(64), nullable=True),
        sa.Column("acknowledged_fingerprint", sa.String(64), nullable=True),
        sa.UniqueConstraint(
            "account_id", "user_id", "exception_key", name="uq_exception_digest_state"
        ),
    )
    for table in (
        "exception_digest_week",
        "exception_digest_delivery",
        "exception_digest_state",
    ):
        op.create_index(f"ix_{table}_id", table, ["id"])


def downgrade() -> None:
    """Remove only the new digest ledger tables."""
    for table in (
        "exception_digest_state",
        "exception_digest_delivery",
        "exception_digest_week",
    ):
        op.drop_table(table)
