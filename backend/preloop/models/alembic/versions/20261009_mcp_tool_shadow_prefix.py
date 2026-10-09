"""MCP tool name collisions: shadowed flag on mcp_tool, optional server prefix.

Revision ID: 20261009_mcp_tool_shadow_prefix
Revises: 20261004_ci_subscription_binding
"""

import sqlalchemy as sa
from alembic import op

revision = "20261009_mcp_tool_shadow_prefix"
down_revision = "20261004_ci_subscription_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``mcp_tool.shadowed`` and ``mcp_server.tool_prefix`` (#1135)."""
    op.add_column(
        "mcp_tool",
        sa.Column("shadowed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "mcp_server", sa.Column("tool_prefix", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    """Drop the collision columns."""
    op.drop_column("mcp_server", "tool_prefix")
    op.drop_column("mcp_tool", "shadowed")
