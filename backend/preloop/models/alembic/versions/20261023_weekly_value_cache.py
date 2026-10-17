"""Cache forge evidence for deterministic weekly outcome attribution."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261023_weekly_value_cache"
down_revision = "20261023_gov_retry_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("value_evidence", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("value_observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("issue_cost_pull_request", "value_observed_at")
    op.drop_column("issue_cost_pull_request", "value_evidence")
