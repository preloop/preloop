"""Persist provider token receipt anchors without inferring refresh lifetime.

Revision ID: 20261009_oauth_receipt_anchor
Revises: 20261004_ci_subscription_binding
"""

from alembic import op
import sqlalchemy as sa

revision = "20261009_oauth_receipt_anchor"
down_revision = "20261004_ci_subscription_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oauth_token", sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("oauth_token", "issued_at")
