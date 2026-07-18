"""Add time-boxed approval bypasses and auto-approval markers.

Revision ID: 20260718_approval_bypass
Revises: 20260714_growth_analytics
Create Date: 2026-07-18

Adds:
* ``approval_bypass`` — time-boxed, attributable suppressions of approval
  gating (``auto_approve``) or of notification fan-out only
  (``mute_notifications``).
* ``approval_request.auto_approved_reason`` / ``auto_approval_bypass_id`` —
  markers so a request auto-approved under a bypass is permanently
  distinguishable from a human decision and from an AI decision.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "20260718_approval_bypass"
down_revision = "20260714_growth_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the approval_bypass table and approval-request markers."""
    approval_bypass_mode = sa.Enum(
        "mute_notifications",
        "auto_approve",
        name="approval_bypass_mode",
    )
    approval_bypass_mode.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "approval_bypass",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
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
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("managed_agent_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "mode",
            approval_bypass_mode,
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_via", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "auto_approved_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_index("ix_approval_bypass_id", "approval_bypass", ["id"], unique=False)
    op.create_index("ix_approval_bypass_account_id", "approval_bypass", ["account_id"])
    op.create_index("ix_approval_bypass_user_id", "approval_bypass", ["user_id"])
    op.create_index(
        "ix_approval_bypass_managed_agent_id",
        "approval_bypass",
        ["managed_agent_id"],
    )
    op.create_index("ix_approval_bypass_mode", "approval_bypass", ["mode"])
    op.create_index("ix_approval_bypass_expires_at", "approval_bypass", ["expires_at"])
    op.create_index(
        "ix_approval_bypass_lookup",
        "approval_bypass",
        ["account_id", "user_id", "expires_at"],
    )

    op.add_column(
        "approval_request",
        sa.Column("auto_approved_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "approval_request",
        sa.Column(
            "auto_approval_bypass_id",
            UUID(as_uuid=True),
            sa.ForeignKey("approval_bypass.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_approval_request_auto_approved_reason",
        "approval_request",
        ["auto_approved_reason"],
    )
    op.create_index(
        "ix_approval_request_auto_approval_bypass_id",
        "approval_request",
        ["auto_approval_bypass_id"],
    )


def downgrade() -> None:
    """Drop the approval_bypass table and approval-request markers."""
    op.drop_index(
        "ix_approval_request_auto_approval_bypass_id",
        table_name="approval_request",
    )
    op.drop_index(
        "ix_approval_request_auto_approved_reason", table_name="approval_request"
    )
    op.drop_column("approval_request", "auto_approval_bypass_id")
    op.drop_column("approval_request", "auto_approved_reason")

    op.drop_index("ix_approval_bypass_lookup", table_name="approval_bypass")
    op.drop_index("ix_approval_bypass_expires_at", table_name="approval_bypass")
    op.drop_index("ix_approval_bypass_mode", table_name="approval_bypass")
    op.drop_index("ix_approval_bypass_managed_agent_id", table_name="approval_bypass")
    op.drop_index("ix_approval_bypass_user_id", table_name="approval_bypass")
    op.drop_index("ix_approval_bypass_account_id", table_name="approval_bypass")
    op.drop_index("ix_approval_bypass_id", table_name="approval_bypass")
    op.drop_table("approval_bypass")

    sa.Enum(name="approval_bypass_mode").drop(op.get_bind(), checkfirst=True)
