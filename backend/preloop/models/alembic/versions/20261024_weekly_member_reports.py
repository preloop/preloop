"""Independent member weekly report and policy disclosure switches."""

from alembic import op
import sqlalchemy as sa

revision = "20261024_weekly_members"
down_revision = "20261022_weekly_report_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Leave member delivery unset so the edition chooses its plan default."""
    op.add_column(
        "account",
        sa.Column("weekly_member_report_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "account",
        sa.Column(
            "weekly_member_policy_findings_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Remove member report controls."""
    op.drop_column("account", "weekly_member_policy_findings_enabled")
    op.drop_column("account", "weekly_member_report_enabled")
