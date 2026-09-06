"""Merge runner capabilities and flow artifact migration branches.

Revision ID: 20260906_runner_artifact_merge
Revises: 20260906_runner_caps, 20260906_flow_artifacts
Create Date: 2026-09-06
"""

revision = "20260906_runner_artifact_merge"
down_revision = ("20260906_runner_caps", "20260906_flow_artifacts")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join both applied histories without changing their schema operations."""
    pass


def downgrade() -> None:
    """Restore both branch heads without undoing either schema."""
    pass
