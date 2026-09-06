"""Merge halt launch-intent and runner-capability histories.

Revision ID: 20260906_halt_runner_merge
Revises: 20260906_halt_launch_intent, 20260906_runner_artifact_merge
"""

revision = "20260906_halt_runner_merge"
down_revision = ("20260906_halt_launch_intent", "20260906_runner_artifact_merge")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve both already-applied migration branches."""
    pass


def downgrade() -> None:
    """Return to the two independent heads without modifying their data."""
    pass
