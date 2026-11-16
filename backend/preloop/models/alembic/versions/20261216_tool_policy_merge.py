"""Join the tool registry and integrated policy branches without changing data."""

revision = "20261216_tool_policy_merge"
down_revision = (
    "20261214_mcp_registry_merge",
    "20261215_flow_policy_merge",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Converge published revisions while retaining registry and policy data."""
    pass


def downgrade() -> None:
    """Restore the parent heads without changing registry or policy data."""
    pass
