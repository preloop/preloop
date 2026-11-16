"""Approve-once registry for MCP tool routes (#2313)."""

from alembic import op
import sqlalchemy as sa

revision = "20261116_tool_registry"
down_revision = "20261212_flow_approval_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the route identity and account/state indexes."""
    op.create_table(
        "tool_registry_entry",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "account_id",
            sa.UUID(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tool_source", sa.String(32), nullable=False),
        sa.Column(
            "mcp_server_id",
            sa.UUID(),
            sa.ForeignKey("mcp_server.id", ondelete="CASCADE"),
        ),
        sa.Column("server_label", sa.String(255)),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "first_seen_agent_id",
            sa.UUID(),
            sa.ForeignKey("managed_agent.id", ondelete="SET NULL"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column(
            "decided_by_user_id",
            sa.UUID(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
        ),
        sa.Column("approval_request_id", sa.UUID()),
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
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'denied')", name="ck_tool_registry_state"
        ),
        sa.UniqueConstraint(
            "account_id",
            "tool_source",
            "mcp_server_id",
            "server_label",
            "tool_name",
            name="uq_tool_registry_entry",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_tool_registry_entry_account_state",
        "tool_registry_entry",
        ["account_id", "state"],
    )


def downgrade() -> None:
    """Remove registry decisions; there are no inbound foreign keys."""
    op.drop_table("tool_registry_entry")
