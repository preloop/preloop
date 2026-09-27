"""Add policy_notice_hit for model I/O rules with the notify action.

Revision ID: 20260927_policy_notice_hit
Revises: 20260927_copilot_import
Create Date: 2026-09-27

One row per notify match (#959). The prompt or completion is never stored:
only its SHA-256 and a secret-scrubbed excerpt of at most 280 characters.
``notified_at`` marks the hit that sent the outbound notice, which is how the
one-message-per-rule-per-user-per-hour debounce works across replicas.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "20260927_policy_notice_hit"
down_revision = "20260927_copilot_import"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the policy_notice_hit table and its lookup indexes."""
    op.create_table(
        "policy_notice_hit",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
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
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "target",
            sa.String(32),
            nullable=False,
            comment="model.request | model.response",
        ),
        sa.Column("rule_id", sa.String(255), nullable=False),
        sa.Column("rule_description", sa.Text(), nullable=True),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column(
            "excerpt",
            sa.Text(),
            nullable=True,
            comment="Secret-scrubbed excerpt around the match, at most 280 chars",
        ),
        sa.Column(
            "notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set when this hit sent the outbound notice (debounce marker)",
        ),
    )
    op.create_index(
        "ix_policy_notice_hit_account_rule_created",
        "policy_notice_hit",
        ["account_id", "rule_id", "created_at"],
    )
    op.create_index(
        "ix_policy_notice_hit_account_created",
        "policy_notice_hit",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    """Drop the policy_notice_hit table."""
    op.drop_index(
        "ix_policy_notice_hit_account_created", table_name="policy_notice_hit"
    )
    op.drop_index(
        "ix_policy_notice_hit_account_rule_created", table_name="policy_notice_hit"
    )
    op.drop_table("policy_notice_hit")
