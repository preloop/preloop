"""Add the per account session search backfill watermark.

The backfill sweeper walks an account's runtime sessions newest first and
indexes the sources that already exist. One row per account records how far
back that walk reached, so a restarted sweeper resumes instead of scanning
history it already indexed, and records when the walk finished, so a completed
account is skipped without touching its sessions at all.

The watermark is a ``(started_at, id)`` pair rather than a timestamp alone:
two sessions can share a start timestamp, and an offset based cursor would
skip or repeat sessions written between two passes.

A partial index on ``updated_at`` where ``completed_at IS NULL`` keeps the
"which accounts still need a pass" query off a full scan of the table once
most accounts are done.

Revision ID: 20260915_session_backfill
Revises: 20260915_session_parent
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260915_session_backfill"
down_revision: Union[str, None] = "20260915_session_parent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the backfill state table and its pending-account index."""
    op.create_table(
        "session_search_backfill_state",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cursor_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pass_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sessions_scanned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_written", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
    )
    op.create_index(
        op.f("ix_session_search_backfill_state_id"),
        "session_search_backfill_state",
        ["id"],
        unique=False,
    )
    # Unique index rather than a named constraint: one row per account is the
    # whole point, and the index is what the per account read uses.
    op.create_index(
        op.f("ix_session_search_backfill_state_account_id"),
        "session_search_backfill_state",
        ["account_id"],
        unique=True,
    )
    op.create_index(
        "ix_session_search_backfill_state_pending",
        "session_search_backfill_state",
        ["updated_at"],
        postgresql_where=sa.text("completed_at IS NULL"),
    )


def downgrade() -> None:
    """Drop the backfill state table. The corpus itself is untouched."""
    op.drop_index(
        "ix_session_search_backfill_state_pending",
        table_name="session_search_backfill_state",
    )
    op.drop_index(
        op.f("ix_session_search_backfill_state_account_id"),
        table_name="session_search_backfill_state",
    )
    op.drop_index(
        op.f("ix_session_search_backfill_state_id"),
        table_name="session_search_backfill_state",
    )
    op.drop_table("session_search_backfill_state")
