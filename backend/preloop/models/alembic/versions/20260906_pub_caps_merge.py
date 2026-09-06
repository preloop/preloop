"""Merge publication capability history onto the approval API key head.

Revision ID: 20260906_pub_caps_merge
Revises: 20260906_approval_api_key, 20260906_publication_caps
"""

revision = "20260906_pub_caps_merge"
down_revision = ("20260906_approval_api_key", "20260906_publication_caps")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve both already-applied migration branches."""
    pass


def downgrade() -> None:
    """Return to the two independent heads without modifying their data."""
    pass
