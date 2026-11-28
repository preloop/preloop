"""Add the append-only extension decision history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20270106_extension_decisions"
down_revision = "20270105_flow_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create extension_decision_event."""
    op.create_table(
        "extension_decision_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_kind", sa.String(64), nullable=False),
        sa.Column("extension_kind", sa.String(16), nullable=False),
        sa.Column("extension_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("previous_state", sa.String(16), nullable=True),
        sa.Column("new_state", sa.String(16), nullable=True),
        sa.Column("content_hash", sa.String(80), nullable=True),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column(
            "decided_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "action IN ('state_changed','pin_added','pin_removed')",
            name="extension_decision_event_action_check",
        ),
    )
    op.create_index(
        "ix_extension_decision_event_id", "extension_decision_event", ["id"]
    )
    op.create_index(
        "ix_extension_decision_event_account_created",
        "extension_decision_event",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    """Drop extension_decision_event."""
    op.drop_table("extension_decision_event")
