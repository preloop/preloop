"""Preserve concurrent execution acceptance and artifact-index migrations."""

revision = "20261004_ci_exec_artifact_merge"
down_revision = ("20261004_ci_execution_binding", "20261004_artifact_avail_idx")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Retain both independently published schema histories."""


def downgrade() -> None:
    """Restore both branches without erasing either schema change."""
