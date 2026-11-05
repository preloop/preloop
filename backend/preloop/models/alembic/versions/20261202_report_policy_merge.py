"""Converge the published report and entitlement migration branches."""

revision = "20261202_report_policy_merge"
down_revision = (
    "20261201_report_runner_merge",
    "20261201_policy_runner_merge",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
