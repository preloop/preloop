"""Durable implementation threads and feedback inbox.

Revision ID: 20260906_flow_feedback
Revises: 20260906_ae_viewed_uniq
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260906_flow_feedback"
down_revision = "20260906_ae_viewed_uniq"
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create independently leased threads and idempotent feedback receipts."""
    op.create_table(
        "flow_thread",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "flow_id",
            UUID(as_uuid=True),
            sa.ForeignKey("flow.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tracker_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tracker.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *[
            sa.Column(name, sa.String(), nullable=False)
            for name in (
                "repository_id",
                "pr_number",
                "pr_url",
                "provider",
                "branch",
                "state",
            )
        ],
        *[
            sa.Column(name, sa.JSON(), nullable=False)
            for name in ("context", "policy", "cursor")
        ],
        sa.Column("stop_reason", sa.String()),
        sa.Column("latest_execution_id", UUID(as_uuid=True), nullable=False),
        sa.Column("active_execution_id", UUID(as_uuid=True)),
        sa.Column("head_sha", sa.String()),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("no_progress", sa.Integer(), nullable=False),
        sa.Column("lease_token", UUID(as_uuid=True)),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "flow_id",
            "tracker_id",
            "repository_id",
            "pr_number",
            name="uq_flow_thread_binding",
        ),
    )
    op.create_index("ix_flow_thread_due_at", "flow_thread", ["due_at"])
    op.create_table(
        "flow_feedback",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "thread_id",
            UUID(as_uuid=True),
            sa.ForeignKey("flow_thread.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("delivery_id", sa.String()),
        sa.Column("head_sha", sa.String()),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("consumed_by", UUID(as_uuid=True)),
        sa.UniqueConstraint("thread_id", "event_key", name="uq_flow_feedback_delivery"),
    )
    op.create_index("ix_flow_feedback_thread_id", "flow_feedback", ["thread_id"])


def downgrade() -> None:
    """Remove subscriptions and their receipts; execution audit remains."""
    op.drop_table("flow_feedback")
    op.drop_table("flow_thread")
