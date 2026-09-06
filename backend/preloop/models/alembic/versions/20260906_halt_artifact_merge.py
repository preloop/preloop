"""Merge independent halt durability and recovery artifact histories.

Revision ID: 20260906_halt_artifact_merge
Revises: 20260906_halt_durability, 20260906_flow_artifacts
"""

revision = "20260906_halt_artifact_merge"
down_revision = ("20260906_halt_durability", "20260906_flow_artifacts")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve both already-applied migration branches."""
    pass


def downgrade() -> None:
    """Return to the two independent heads without modifying their data."""
    pass
