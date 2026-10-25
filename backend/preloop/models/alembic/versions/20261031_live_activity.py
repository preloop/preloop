"""Live activity: hook turn-event index, rollups and presence state (#1889).

Revision ID: 20261031_live_activity
Revises: 20261030_gateway_model_idx

Phase 0 indexes, so the live activity worker reads its sources without
scanning raw tables:

* ``ix_rsa_account_turn_events``: partial on ``runtime_session_activity``
  for the ``user_prompt``/``turn_end``/``session_end`` rows the hooks now
  write, keyed ``(timestamp, account_id)`` so the worker's cross-account
  range read uses it; small next to the tool-call volume.
* the partial ``ix_approval_request_resolved_at``: the worker reads decided
  approvals by decision time (request time already has
  ``ix_approval_request_requested_at`` from the initial schema).

Phase 1 tables, written only by the extension plugin and empty in the
open-source edition: ``activity_rollup``, ``presence_state``,
``activity_worker_watermark``, ``activity_settings``.

The indexes on existing tables are built CONCURRENTLY (IF NOT EXISTS, with
an invalid leftover dropped first) so gateway and hook writes never block.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision: str = "20261031_live_activity"
down_revision: Union[str, None] = "20261030_gateway_model_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONCURRENT_INDEXES = {
    "ix_rsa_account_turn_events": (
        "ON runtime_session_activity (timestamp, account_id) "
        "WHERE activity_type IN ('user_prompt', 'turn_end', 'session_end')"
    ),
    "ix_approval_request_resolved_at": (
        "ON approval_request (resolved_at) WHERE resolved_at IS NOT NULL"
    ),
}


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    """Build concurrent source indexes, then create the tables transactionally."""
    # Build existing-table indexes before creating the feature tables. Entering
    # autocommit commits prior DDL; a canceled build must leave no tables behind
    # that would make the next upgrade fail with DuplicateTable.
    with concurrent_index_ddl(op.get_context()):
        bind = op.get_bind()
        for name, definition in CONCURRENT_INDEXES.items():
            valid = bind.execute(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                    "ON c.oid = i.indexrelid WHERE c.relname = :name "
                    "AND c.relnamespace = current_schema()::regnamespace"
                ),
                {"name": name},
            ).scalar()
            if valid is False:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            op.execute(
                sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")
            )

    op.create_table(
        "activity_rollup",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("granularity", sa.SmallInteger(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        *[
            sa.Column(name, sa.Integer(), nullable=False, server_default="0")
            for name in (
                "requests",
                "tool_calls",
                "prompts",
                "turn_ends",
                "approvals_requested",
                "approvals_decided",
            )
        ],
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "completion_tokens", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("spend_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column(
            "reply_latency_hist", postgresql.ARRAY(sa.SmallInteger()), nullable=False
        ),
        sa.Column(
            "approval_latency_hist",
            postgresql.ARRAY(sa.SmallInteger()),
            nullable=False,
        ),
        sa.Column("active_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_pass_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "account_id",
            "granularity",
            "subject_type",
            "subject_id",
            "bucket_start",
            name="uq_activity_rollup_key",
        ),
    )
    op.create_index("ix_activity_rollup_id", "activity_rollup", ["id"])
    op.create_index(
        "ix_activity_rollup_account_bucket",
        "activity_rollup",
        ["account_id", "granularity", sa.text("bucket_start DESC")],
    )

    op.create_table(
        "presence_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_signal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_signal_kind", sa.String(16), nullable=True),
        sa.Column("awaiting_reply_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "awaiting_approval_count",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("runtime_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("device_key", sa.String(128), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_presence_state_id", "presence_state", ["id"])
    op.create_index(
        "uq_presence_state_current",
        "presence_state",
        ["account_id", "subject_type", "subject_id"],
        unique=True,
        postgresql_where=sa.text("until IS NULL"),
    )
    op.create_index(
        "ix_presence_state_account_since",
        "presence_state",
        ["account_id", "subject_type", sa.text("since DESC")],
    )

    op.create_table(
        "activity_worker_watermark",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False, unique=True),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "ix_activity_worker_watermark_id", "activity_worker_watermark", ["id"]
    )

    op.create_table(
        "activity_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "presence_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        *_timestamps(),
    )
    op.create_index("ix_activity_settings_id", "activity_settings", ["id"])


def downgrade() -> None:
    """Drop the indexes and the four tables."""
    with concurrent_index_ddl(op.get_context()):
        for name in CONCURRENT_INDEXES:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
    op.drop_table("activity_settings")
    op.drop_table("activity_worker_watermark")
    op.drop_table("presence_state")
    op.drop_table("activity_rollup")
