"""Account override for optional governance report model analysis.

Revision ID: 20261130_report_analysis_flag
Revises: 20261129_retention_exec_idx
"""

from alembic import op
import sqlalchemy as sa

revision = "20261130_report_analysis_flag"
down_revision = "20261129_retention_exec_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "governance_review_settings",
        sa.Column("model_analysis_override", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("governance_review_settings", "model_analysis_override")
