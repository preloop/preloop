"""Join extension inventory with the preceding account policy stack."""

revision = "20261215_extensions_merge"
down_revision = ("20261212_extensions", "20261214_mcp_registry_merge")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join both published migration branches without changing their data."""


def downgrade() -> None:
    """Restore the independent branch heads."""
