"""Join the MCP registry and preceding policy migration chains."""

revision = "20261216_tool_policy_merge"
down_revision = ("20261214_mcp_registry_merge", "20261215_flow_policy_merge")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Converge the published policy branches without changing data."""


def downgrade() -> None:
    """Restore the independent branch heads."""
