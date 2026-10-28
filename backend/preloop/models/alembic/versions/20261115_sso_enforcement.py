"""Carry membership-local session assurance for SSO enforcement.

Revision ID: 20261115_sso_enforcement
Revises: 20261114_sso_group_grants
"""

from alembic import op
import sqlalchemy as sa

revision = "20261115_sso_enforcement"
down_revision = "20261114_sso_group_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sso_break_glass_notice",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "account_id",
            sa.UUID(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("login_id", sa.UUID(), nullable=False),
        sa.Column("actor_email", sa.String(320), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("login_id", "recipient_email"),
    )
    op.add_column(
        "user",
        sa.Column(
            "non_sso_auth_generation", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    for table in (
        "oauth_mcp_authorization_code",
        "oauth_mcp_access_token",
        "oauth_mcp_refresh_token",
    ):
        op.add_column(
            table,
            sa.Column(
                "auth_context",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "auth_generation", sa.Integer(), nullable=False, server_default="0"
            ),
        )


def downgrade() -> None:
    op.drop_table("sso_break_glass_notice")
    for table in (
        "oauth_mcp_refresh_token",
        "oauth_mcp_access_token",
        "oauth_mcp_authorization_code",
    ):
        op.drop_column(table, "auth_generation")
        op.drop_column(table, "auth_context")
    op.drop_column("user", "non_sso_auth_generation")
