"""Add pull request refresh bookkeeping and forge webhook delivery dedupe (#2017).

Adds ``refresh_due_at``, ``refreshed_at`` and ``refresh_etag`` to
``issue_cost_pull_request`` and creates ``forge_webhook_delivery``. Additive;
no backfill: a NULL ``refresh_due_at`` on an open or unobserved pull request
already means "due".

Revision ID: 20261112_forge_reconciliation
Revises: 20261110_upstream_mcp_oauth
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261112_forge_reconciliation"
down_revision = "20261110_upstream_mcp_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the refresh columns, their due index and the delivery table."""
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("refresh_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "issue_cost_pull_request",
        sa.Column("refresh_etag", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_issue_cost_pull_request_refresh_due",
        "issue_cost_pull_request",
        ["refresh_due_at"],
    )
    op.create_table(
        "forge_webhook_delivery",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tracker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tracker.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "tracker_id", "delivery_id", name="uq_forge_webhook_delivery"
        ),
    )
    op.create_index("ix_forge_webhook_delivery_id", "forge_webhook_delivery", ["id"])
    op.create_index(
        "ix_forge_webhook_delivery_received_at",
        "forge_webhook_delivery",
        ["received_at"],
    )


def downgrade() -> None:
    """Drop the delivery table and the refresh columns."""
    op.drop_table("forge_webhook_delivery")
    op.drop_index(
        "ix_issue_cost_pull_request_refresh_due", table_name="issue_cost_pull_request"
    )
    op.drop_column("issue_cost_pull_request", "refresh_etag")
    op.drop_column("issue_cost_pull_request", "refreshed_at")
    op.drop_column("issue_cost_pull_request", "refresh_due_at")
