"""Connection-local SCIM resources and durable token admission.

Revision ID: 20261118_scim_provisioning
Revises: 20261117_membership_offboarding
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261118_scim_provisioning"
down_revision = "20261117_membership_offboarding"
branch_labels = None
depends_on = None


def common_columns() -> list[sa.Column]:
    """Match the shared model base's identity and UTC timestamps."""
    return [
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    """Keep legacy tokens unbound and therefore unable to provision."""
    op.add_column(
        "scim_token",
        sa.Column(
            "connection_id",
            sa.UUID(),
            sa.ForeignKey("account_sso_connection.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_scim_token_connection_id", "scim_token", ["connection_id"])
    op.add_column(
        "scim_token", sa.Column("rate_window_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "scim_token",
        sa.Column("rate_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "scim_resource",
        *common_columns(),
        sa.Column(
            "connection_id",
            sa.UUID(),
            sa.ForeignKey("account_sso_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "mapping_id",
            sa.UUID(),
            sa.ForeignKey("sso_group_mapping.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name_normalized", sa.String(512), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=True),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "connection_id", "kind", "name_normalized", name="uq_scim_resource_name"
        ),
        sa.UniqueConstraint(
            "connection_id", "kind", "external_id", name="uq_scim_resource_external"
        ),
        sa.UniqueConstraint("connection_id", "user_id", name="uq_scim_resource_user"),
        sa.UniqueConstraint(
            "connection_id", "mapping_id", name="uq_scim_resource_mapping"
        ),
        sa.CheckConstraint(
            "(kind = 'Users' AND user_id IS NOT NULL AND mapping_id IS NULL) OR (kind = 'Groups' AND mapping_id IS NOT NULL AND user_id IS NULL)",
            name="ck_scim_resource_target",
        ),
    )
    op.create_index("ix_scim_resource_id", "scim_resource", ["id"])
    op.create_index(
        "ix_scim_resource_connection_id", "scim_resource", ["connection_id"]
    )
    op.create_table(
        "scim_group_member",
        *common_columns(),
        sa.Column(
            "group_id",
            sa.UUID(),
            sa.ForeignKey("scim_resource.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "member_id",
            sa.UUID(),
            sa.ForeignKey("scim_resource.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("group_id", "member_id", name="uq_scim_group_member"),
    )
    for column in ("id", "group_id", "member_id"):
        op.create_index(f"ix_scim_group_member_{column}", "scim_group_member", [column])


def downgrade() -> None:
    """Remove only SCIM provisioning state."""
    op.drop_table("scim_group_member")
    op.drop_table("scim_resource")
    op.drop_column("scim_token", "rate_count")
    op.drop_column("scim_token", "rate_window_at")
    op.drop_index("ix_scim_token_connection_id", table_name="scim_token")
    op.drop_column("scim_token", "connection_id")
