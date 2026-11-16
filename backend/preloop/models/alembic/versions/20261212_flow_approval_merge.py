"""Join flow webhook rotation and external approval migrations."""

revision = "20261212_flow_approval_merge"
down_revision = (
    "20261210_flow_webhook_rotation",
    "20261211_external_grant_idx",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the published branches without changing any stored credentials."""
    pass


def downgrade() -> None:
    """Restore the independent branch heads without changing data."""
    pass
