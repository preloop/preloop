"""Idempotency record for reviews the control plane posts itself.

Revision ID: 20261208_platform_review
Revises: 20261207_user_passkey_only

One row per execution of a read-only review flow that opted into
``platform_review``. The migration writes no rows. Rows are created only when
a publication is attempted; the automatic sweep considers only executions of
CI identities provisioned with the opt-in on.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261208_platform_review"
down_revision: Union[str, None] = "20261207_user_passkey_only"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "platform_review_publication"


def upgrade() -> None:
    """Create the table with one row per execution."""
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_execution.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime()),
        sa.Column("next_attempt_at", sa.DateTime()),
        sa.Column("repository", sa.String(512), nullable=False),
        sa.Column("pull_request", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("review_state", sa.String(16)),
        sa.Column("review_action", sa.String(32)),
        sa.Column("review_id", sa.String(128)),
        sa.Column("review_reason", sa.String(64)),
        sa.Column("status_state", sa.String(16)),
        sa.Column("status_value", sa.String(16)),
        sa.Column("status_context", sa.String(128)),
        sa.Column("status_id", sa.String(128)),
        sa.Column("status_reason", sa.String(64)),
        sa.Column("posted_at", sa.DateTime()),
        sa.UniqueConstraint(
            "execution_id", name="uq_platform_review_publication_execution"
        ),
    )
    for name in ("id", "account_id", "execution_id", "state", "next_attempt_at"):
        op.create_index(f"ix_{TABLE}_{name}", TABLE, [name])


def downgrade() -> None:
    """Drop the table; posted reviews stay on the provider."""
    op.drop_table(TABLE)
