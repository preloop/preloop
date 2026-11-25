"""Account extension enforcement policy.

Revision ID: 20261230_ext_enforcement
Revises: 20261229_azure_reviews
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261230_ext_enforcement"
down_revision = "20261229_azure_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable policy inputs independently of discovery retention."""
    op.create_table(
        "extension_enforcement_policy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_kind", sa.String(64), nullable=False),
        sa.Column(
            "marketplaces", postgresql.JSONB(), server_default="[]", nullable=False
        ),
        sa.Column("options", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
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
        sa.UniqueConstraint(
            "account_id", "agent_kind", name="uq_extension_enforcement_policy"
        ),
    )
    op.create_index(
        "ix_extension_enforcement_policy_id", "extension_enforcement_policy", ["id"]
    )


def downgrade() -> None:
    """Remove only policy inputs; deployed files require explicit removal."""
    op.drop_table("extension_enforcement_policy")
