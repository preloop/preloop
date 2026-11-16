"""Join the MCP registry with the predecessor sign-in policy chain."""

revision = "20261214_mcp_registry_merge"
down_revision = (
    "20261116_tool_registry",
    "20261213_flow_signin_merge",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Converge published branches without changing policy or account data."""
    pass


def downgrade() -> None:
    """Restore the branch heads without changing registry decisions."""
    pass
