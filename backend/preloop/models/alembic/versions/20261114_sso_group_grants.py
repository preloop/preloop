"""Track independent SSO source claims without revoking manual assignments."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261114_sso_group_grants"
down_revision = "20261113_pull_request_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Existing assignments remain manual; new SSO assignments carry claims."""
    for table, name, condition in (
        (
            "account_sso_connection",
            "ck_account_sso_connection_not_owner",
            "lower(btrim(default_role)) <> 'owner'",
        ),
        (
            "sso_group_mapping",
            "ck_sso_group_mapping_not_owner",
            "role IS NULL OR lower(btrim(role)) <> 'owner'",
        ),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition)
    for table in ("user_role", "team_membership"):
        op.add_column(
            table,
            sa.Column(
                "manually_granted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    op.create_table(
        "sso_provisioning_grant",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account_sso_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "user_role_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user_role.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "team_membership_id",
            UUID(as_uuid=True),
            sa.ForeignKey("team_membership.id", ondelete="CASCADE"),
        ),
        sa.CheckConstraint(
            "(user_role_id IS NOT NULL) <> (team_membership_id IS NOT NULL)",
            name="ck_sso_provisioning_grant_target",
        ),
        sa.CheckConstraint(
            "source IN ('oidc', 'saml', 'scim')", name="ck_sso_grant_source"
        ),
        sa.UniqueConstraint(
            "connection_id", "source", "user_role_id", name="uq_sso_grant_role"
        ),
        sa.UniqueConstraint(
            "connection_id", "source", "team_membership_id", name="uq_sso_grant_team"
        ),
    )
    for column in (
        "id",
        "connection_id",
        "user_id",
        "user_role_id",
        "team_membership_id",
    ):
        op.create_index(
            f"ix_sso_provisioning_grant_{column}", "sso_provisioning_grant", [column]
        )


def downgrade() -> None:
    """Drop provenance while preserving physical access assignments."""
    for table, name, condition in (
        (
            "account_sso_connection",
            "ck_account_sso_connection_not_owner",
            "default_role <> 'owner'",
        ),
        (
            "sso_group_mapping",
            "ck_sso_group_mapping_not_owner",
            "role IS NULL OR role <> 'owner'",
        ),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition)
    op.drop_table("sso_provisioning_grant")
    for table in ("team_membership", "user_role"):
        op.drop_column(table, "manually_granted")
