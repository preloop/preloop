"""Gateway subjects behind a trusted upstream gateway key.

Revision ID: 20261009_gateway_subject
Revises: 20261009_access_rule_generation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261009_gateway_subject"
down_revision = "20261009_access_rule_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the ``gateway_subject`` table."""
    op.create_table(
        "gateway_subject",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_key.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "linked_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "account_id",
            "api_key_id",
            "external_subject",
            name="uq_gateway_subject_account_key_subject",
        ),
    )
    op.create_index("ix_gateway_subject_id", "gateway_subject", ["id"])
    op.create_index("ix_gateway_subject_account_id", "gateway_subject", ["account_id"])
    op.create_index("ix_gateway_subject_api_key_id", "gateway_subject", ["api_key_id"])
    op.create_index(
        "ix_gateway_subject_linked_user_id", "gateway_subject", ["linked_user_id"]
    )


def downgrade() -> None:
    """Drop the ``gateway_subject`` table."""
    op.drop_index("ix_gateway_subject_linked_user_id", table_name="gateway_subject")
    op.drop_index("ix_gateway_subject_api_key_id", table_name="gateway_subject")
    op.drop_index("ix_gateway_subject_account_id", table_name="gateway_subject")
    op.drop_index("ix_gateway_subject_id", table_name="gateway_subject")
    op.drop_table("gateway_subject")
