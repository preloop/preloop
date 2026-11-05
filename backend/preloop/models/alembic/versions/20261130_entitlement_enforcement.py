"""Persist global and per-account entitlement enforcement controls.

Revision ID: 20261130_entitlement_enforcement
Revises: 20261129_retention_exec_idx
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261130_entitlement_enforcement"
down_revision = "20261129_retention_exec_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entitlement_enforcement_setting",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("scope", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "(scope = 'global' AND account_id IS NULL) OR "
            "(account_id IS NOT NULL AND scope = CAST(account_id AS VARCHAR))",
            name="ck_entitlement_enforcement_scope",
        ),
    )
    op.create_index(
        "ix_entitlement_enforcement_setting_id",
        "entitlement_enforcement_setting",
        ["id"],
    )


def downgrade() -> None:
    op.drop_table("entitlement_enforcement_setting")
