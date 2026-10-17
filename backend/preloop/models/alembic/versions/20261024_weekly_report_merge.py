"""Keep governance retry indexes when adding weekly member report settings."""

revision = "20261024_weekly_report_merge"
down_revision = ("20261024_weekly_members", "20261023_gov_retry_idx")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the independently published migration branches."""


def downgrade() -> None:
    """No schema changes to undo at the merge point."""
