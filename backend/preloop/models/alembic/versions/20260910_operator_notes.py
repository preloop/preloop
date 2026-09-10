"""Operator notes on the agent-control store.

Revision ID: 20260910_operator_notes
Revises: 20260908_webhook_delivery_key
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_operator_notes"
down_revision: Union[str, None] = "20260908_webhook_delivery_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

TABLE = "agent_control_command"
STATUS_CHECK = "ck_agent_control_command_status"
KIND_CHECK = "ck_agent_control_command_kind"
SESSION_INDEX = "ix_agent_control_note_pending_session"
AGENT_INDEX = "ix_agent_control_note_pending_agent"


def upgrade() -> None:
    """Add the note columns, the note indexes and the widened status check.

    Additive: every existing row is a command, which is what the server
    default says, so no backfill is needed and no reader has to change.
    """
    # A note can name a runtime session that has no managed agent behind it
    # (a flow execution on an account credential). Commands always set it.
    op.alter_column(
        TABLE,
        "managed_agent_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        TABLE,
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="command",
        ),
    )
    op.add_column(TABLE, sa.Column("body", sa.Text(), nullable=True))
    op.add_column(
        TABLE, sa.Column("author_display", sa.String(length=255), nullable=True)
    )
    op.add_column(
        TABLE, sa.Column("author_auth_method", sa.String(length=32), nullable=True)
    )
    op.add_column(
        TABLE, sa.Column("delivery_channel", sa.String(length=24), nullable=True)
    )
    op.add_column(TABLE, sa.Column("delivered_turn_index", sa.Integer(), nullable=True))
    op.add_column(
        TABLE, sa.Column("acknowledged_turn_id", sa.String(length=128), nullable=True)
    )
    op.add_column(
        TABLE,
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "cancelled_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_agent_control_command_cancelled_by_user",
        TABLE,
        "user",
        ["cancelled_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # The per-request "any pending note?" lookup, kept off the command history.
    op.create_index(
        SESSION_INDEX,
        TABLE,
        ["runtime_session_id", "status"],
        postgresql_where=sa.text("kind = 'note'"),
    )
    op.create_index(
        AGENT_INDEX,
        TABLE,
        ["managed_agent_id", "status"],
        postgresql_where=sa.text("kind = 'note'"),
    )

    # 'cancelled' is a note state: a withdrawn note keeps its row.
    op.drop_constraint(STATUS_CHECK, TABLE, type_="check")
    op.create_check_constraint(
        STATUS_CHECK,
        TABLE,
        "status IN ('pending', 'delivered', 'acked', 'failed', 'expired', 'cancelled')",
    )
    op.create_check_constraint(KIND_CHECK, TABLE, "kind IN ('command', 'note')")


def downgrade() -> None:
    """Drop the note columns and restore the original status check.

    Note rows are deleted, because a downgraded reader would treat them as
    commands and try to push them down the control WebSocket.
    """
    op.execute(sa.text("DELETE FROM agent_control_command WHERE kind = 'note'"))
    op.drop_constraint(KIND_CHECK, TABLE, type_="check")
    op.drop_constraint(STATUS_CHECK, TABLE, type_="check")
    op.create_check_constraint(
        STATUS_CHECK,
        TABLE,
        "status IN ('pending', 'delivered', 'acked', 'failed', 'expired')",
    )
    op.drop_index(AGENT_INDEX, table_name=TABLE)
    op.drop_index(SESSION_INDEX, table_name=TABLE)
    op.drop_constraint(
        "fk_agent_control_command_cancelled_by_user", TABLE, type_="foreignkey"
    )
    op.drop_column(TABLE, "cancelled_by_user_id")
    op.drop_column(TABLE, "cancelled_at")
    op.drop_column(TABLE, "acknowledged_turn_id")
    op.drop_column(TABLE, "delivered_turn_index")
    op.drop_column(TABLE, "delivery_channel")
    op.drop_column(TABLE, "author_auth_method")
    op.drop_column(TABLE, "author_display")
    op.drop_column(TABLE, "body")
    op.drop_column(TABLE, "kind")
    op.alter_column(
        TABLE,
        "managed_agent_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
