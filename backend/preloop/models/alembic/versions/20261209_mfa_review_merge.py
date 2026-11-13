"""Join authenticator storage with the platform review migration.

Revision ID: 20261209_mfa_review_merge
Revises: 20261208_platform_review, 20261208_user_mfa
"""

revision = "20261209_mfa_review_merge"
down_revision = ("20261208_platform_review", "20261208_user_mfa")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join both published branches without changing their tables."""


def downgrade() -> None:
    """Restore the sibling heads without removing either branch's data."""
