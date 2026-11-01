"""Membership-local offboarding and deletion-safe credential attribution.

Revision ID: 20261117_membership_offboarding
Revises: 20261122_execution_result_grant
"""

from alembic import op
import sqlalchemy as sa

revision = "20261117_membership_offboarding"
down_revision = "20261122_execution_result_grant"
branch_labels = None
depends_on = None


def _drop_creator_fk() -> None:
    for constraint in sa.inspect(op.get_bind()).get_foreign_keys("api_key"):
        if constraint["constrained_columns"] == ["user_id"]:
            op.drop_constraint(constraint["name"], "api_key", type_="foreignkey")


def upgrade() -> None:
    op.add_column(
        "user", sa.Column("offboarded_at", sa.DateTime(timezone=True), nullable=True)
    )
    _drop_creator_fk()
    op.alter_column("api_key", "user_id", nullable=True)
    op.create_foreign_key(
        "fk_api_key_creator_user",
        "api_key",
        "user",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM api_key WHERE user_id IS NULL)")
    ):
        raise RuntimeError(
            "Cannot restore mandatory credential creators while retained credentials have deleted creators"
        )
    _drop_creator_fk()
    op.alter_column("api_key", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_api_key_creator_user",
        "api_key",
        "user",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("user", "offboarded_at")
