"""Add attention_dismissal table for console attention inbox mutes.

Revision ID: 20260902_attention_dismiss
Revises: 20260830_user_avatar
Create Date: 2026-09-02

One row per (account, attention item). ``item_id`` is the console's stable
``<kind>:<entity id>`` string rather than a foreign key, because attention
items are derived on the client and can be about an agent, a flow, a model,
a budget or the price catalog. ``fingerprint`` records why the item was
showing so a changed cause un-hides it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "20260902_attention_dismiss"
down_revision = "20260830_user_avatar"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the attention_dismissal table."""
    op.create_table(
        "attention_dismissal",
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
            "item_id",
            sa.String(255),
            nullable=False,
            comment="Stable console item id: '<kind>:<entity id>'",
        ),
        sa.Column(
            "fingerprint",
            sa.Text(),
            nullable=False,
            comment="Why the item was showing; a change un-hides the item",
        ),
        sa.Column(
            "reason",
            sa.String(16),
            nullable=False,
            comment="expected | snoozed | fixed",
        ),
        sa.Column(
            "snooze_until",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set for reason='snoozed'; the row is inert once passed",
        ),
        sa.Column(
            "dismissed_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "account_id", "item_id", name="uq_attention_dismissal_account_item"
        ),
    )
    op.create_index(
        "ix_attention_dismissal_account_id",
        "attention_dismissal",
        ["account_id"],
    )


def downgrade() -> None:
    """Drop the attention_dismissal table."""
    op.drop_index("ix_attention_dismissal_account_id", table_name="attention_dismissal")
    op.drop_table("attention_dismissal")
