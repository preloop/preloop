"""Join extension inventory and grant evidence indexes without changing data."""

revision = "20261219_ext_grant_merge"
down_revision = ("20261218_extension_obs_idx", "20261217_grant_evidence_idx")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep one upgrade target while preserving both published chains."""


def downgrade() -> None:
    """Restore the extension inventory and grant evidence heads."""
