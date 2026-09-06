"""Merge publication-capability and issue-lifecycle histories.

Revision ID: 20260906_lifecycle_key_merge
Revises: 20260906_pub_caps_merge, 20260906_issue_lifecycle
"""

revision = "20260906_lifecycle_key_merge"
down_revision = ("20260906_pub_caps_merge", "20260906_issue_lifecycle")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve both already-applied migration branches."""
    pass


def downgrade() -> None:
    """Return to the two independent heads without modifying their data."""
    pass
