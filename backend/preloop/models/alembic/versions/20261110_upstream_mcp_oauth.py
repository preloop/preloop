"""Add durable upstream MCP authorization and refresh coordination.

Revision ID: 20261110_upstream_mcp_oauth
Revises: 20261109_agent_usage_totals
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20261110_upstream_mcp_oauth"
down_revision = "20261109_agent_usage_totals"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "upstream_mcp_oauth_connection",
        *_base(),
        sa.Column(
            "server_id",
            UUID(as_uuid=True),
            sa.ForeignKey("mcp_server.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column(
            "secret_id",
            UUID(as_uuid=True),
            sa.ForeignKey("secret_reference.id", ondelete="SET NULL"),
        ),
        sa.Column("lease_id", UUID(as_uuid=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "server_id", "account_id", name="uq_upstream_mcp_oauth_owner"
        ),
    )
    op.create_index(
        "ix_upstream_mcp_oauth_connection_account_id",
        "upstream_mcp_oauth_connection",
        ["account_id"],
    )
    op.create_table(
        "upstream_mcp_oauth_transaction",
        *_base(),
        sa.Column("server_id", UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("auth_generation", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("redirect_uri", sa.String(2048), nullable=False),
        sa.Column("context_encrypted", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["server_id", "account_id"],
            [
                "upstream_mcp_oauth_connection.server_id",
                "upstream_mcp_oauth_connection.account_id",
            ],
            ondelete="CASCADE",
            name="fk_upstream_mcp_oauth_transaction_owner",
        ),
    )
    op.create_index(
        "ix_upstream_mcp_oauth_transaction_account_id",
        "upstream_mcp_oauth_transaction",
        ["account_id"],
    )
    op.create_index(
        "ix_upstream_mcp_oauth_transaction_expires_at",
        "upstream_mcp_oauth_transaction",
        ["expires_at"],
    )


def downgrade() -> None:
    # Delete only references owned by this lifecycle before removing its tables.
    op.execute(
        "DELETE FROM secret_reference WHERE id IN (SELECT secret_id FROM upstream_mcp_oauth_connection) AND secret_kind = 'upstream_mcp_oauth'"
    )
    op.drop_table("upstream_mcp_oauth_transaction")
    op.drop_table("upstream_mcp_oauth_connection")
