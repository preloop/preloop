"""Join extension inventory to the updated predecessor stack."""

revision = "20261217_extensions_stack"
down_revision = ("20261215_extensions_merge", "20261216_tool_policy_merge")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep a single upgrade target across the published stack."""


def downgrade() -> None:
    """Restore the inventory and policy heads."""
