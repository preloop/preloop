"""Join concurrent CI identity and Copilot attribution schema branches.

Revision ID: 20261004_ci_copilot_merge
Revises: 20261004_ci_principal, 20261004_copilot_user_mapping
"""

revision = "20261004_ci_copilot_merge"
down_revision = ("20261004_ci_principal", "20261004_copilot_user_mapping")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both parent migrations retain their independent schema changes."""


def downgrade() -> None:
    """Restore both branches without changing either parent's schema."""
