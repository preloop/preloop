"""Persist explicit Azure review requests and coalesced follow-ups."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20261229_azure_reviews"
down_revision = "20261228_flow_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable requests; provider credentials stay on the tracker."""
    op.create_table(
        "azure_review_request",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "flow_id",
            UUID(),
            sa.ForeignKey("flow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tracker_id",
            UUID(),
            sa.ForeignKey("tracker.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repository_id", sa.String(36), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event", JSONB(), nullable=False),
        sa.Column("signals", JSONB(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "flow_id", "repository_id", "number", name="uq_azure_review_request"
        ),
    )
    op.create_index(
        "ix_azure_review_request_due_at", "azure_review_request", ["due_at"]
    )
    op.create_index("ix_azure_review_request_id", "azure_review_request", ["id"])


def downgrade() -> None:
    """Remove pending Azure requests."""
    op.drop_table("azure_review_request")
