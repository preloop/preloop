"""Add account_halt table for the org-level kill switch.

Revision ID: 20260906_account_halt
Revises: 20260906_flow_feedback
Create Date: 2026-09-06

One row per (account, scope) where scope is one of gateway | tools | flows.
Rows persist across activations so each row is both the current kill-switch
state for its scope and the audit record of the latest activation
(actor, timestamp, reason) and deactivation.
"""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "20260906_account_halt"
down_revision = "20260906_flow_feedback"
branch_labels: Optional[str] = None
depends_on: Optional[str] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the account_halt table."""
    op.create_table(
        "account_halt",
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
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "activated_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column(
            "deactivated_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "account_id", "scope", name="uq_account_halt_account_scope"
        ),
        sa.CheckConstraint(
            "scope IN ('gateway', 'tools', 'flows')", name="ck_account_halt_scope"
        ),
    )
    op.create_index("ix_account_halt_account_id", "account_halt", ["account_id"])


def downgrade() -> None:
    """Drop the account_halt table."""
    op.drop_index("ix_account_halt_account_id", table_name="account_halt")
    op.drop_table("account_halt")
