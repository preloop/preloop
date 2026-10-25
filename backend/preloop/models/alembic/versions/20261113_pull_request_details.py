"""Store PR title, body and file list on the pull request ledger.

Revision ID: 20261113_pull_request_details
Revises: 20261112_enterprise_sso
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261113_pull_request_details"
down_revision = "20261112_enterprise_sso"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable detail columns; NULL means not observed yet."""
    op.add_column(
        "issue_cost_pull_request", sa.Column("title", sa.String(500), nullable=True)
    )
    op.add_column(
        "issue_cost_pull_request", sa.Column("body", sa.Text(), nullable=True)
    )
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("author_login", sa.String(255), nullable=True),
    )
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("changed_files", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("files_changed", sa.Integer(), nullable=True),
    )
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("details_observed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the detail columns."""
    for column in (
        "details_observed_at",
        "files_changed",
        "changed_files",
        "author_login",
        "body",
        "title",
    ):
        op.drop_column("issue_cost_pull_request", column)
