"""Signed outbound event webhooks: endpoints plus a delivery outbox.

Revision ID: 20260908_event_webhooks
Revises: 20260908_structured_answer
Create Date: 2026-09-08

``webhook_endpoint`` holds the operator-registered targets (and the
compatibility shim rows for ``approval_config.webhook_url``).
``webhook_delivery`` is the outbox the delivery worker drains: one row per
(endpoint, event, replay generation), carrying the literal body to POST.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260908_event_webhooks"
down_revision: Union[str, None] = "20260908_structured_answer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the endpoint registry and the delivery outbox."""
    op.create_table(
        "webhook_endpoint",
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
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("secret_hint", sa.String(16), nullable=False, server_default=""),
        sa.Column("event_types", JSONB(), nullable=False, server_default="[]"),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(32), nullable=False, server_default="account"),
        sa.Column(
            "approval_workflow_id",
            UUID(as_uuid=True),
            sa.ForeignKey("approval_workflow.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("circuit_opened_at", sa.DateTime(), nullable=True),
        sa.Column("last_delivery_status", sa.String(16), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(), nullable=True),
        sa.Column("last_response_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
    )
    op.create_index(
        "ix_webhook_endpoint_account_id", "webhook_endpoint", ["account_id"]
    )
    op.create_index(
        "ix_webhook_endpoint_approval_workflow_id",
        "webhook_endpoint",
        ["approval_workflow_id"],
    )

    op.create_table(
        "webhook_delivery",
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
            "endpoint_id",
            UUID(as_uuid=True),
            sa.ForeignKey("webhook_endpoint.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("subject_id", UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "endpoint_id", "event_id", "generation", name="uq_webhook_delivery_event"
        ),
    )
    op.create_index(
        "ix_webhook_delivery_account_id", "webhook_delivery", ["account_id"]
    )
    op.create_index(
        "ix_webhook_delivery_endpoint_id", "webhook_delivery", ["endpoint_id"]
    )
    op.create_index(
        "ix_webhook_delivery_due", "webhook_delivery", ["status", "next_attempt_at"]
    )
    op.create_index(
        "ix_webhook_delivery_account_status",
        "webhook_delivery",
        ["account_id", "status"],
    )


def downgrade() -> None:
    """Drop the outbox first, then the endpoints it references."""
    op.drop_index("ix_webhook_delivery_account_status", table_name="webhook_delivery")
    op.drop_index("ix_webhook_delivery_due", table_name="webhook_delivery")
    op.drop_index("ix_webhook_delivery_endpoint_id", table_name="webhook_delivery")
    op.drop_index("ix_webhook_delivery_account_id", table_name="webhook_delivery")
    op.drop_table("webhook_delivery")
    op.drop_index(
        "ix_webhook_endpoint_approval_workflow_id", table_name="webhook_endpoint"
    )
    op.drop_index("ix_webhook_endpoint_account_id", table_name="webhook_endpoint")
    op.drop_table("webhook_endpoint")
