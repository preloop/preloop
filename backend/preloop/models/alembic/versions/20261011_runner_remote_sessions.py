"""Runner remote sessions: harness sessions hosted by personal runners.

Revision ID: 20261011_runner_remote_sessions
Revises: 20261010_otlp_telemetry_ingest
Create Date: 2026-10-11

Issue #1482. One row per remote session a runner hosts (the
``remote_session_id`` on the runner websocket), paired with a
``runtime_session`` row of source type ``runner_session``. The workspace spec
is stored without any clone credential.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261011_runner_remote_sessions"
down_revision: Union[str, None] = "20261010_otlp_telemetry_ingest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    op.create_table(
        "runner_remote_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_session.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("flow_runner.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("harness", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("workspace", postgresql.JSONB(), nullable=False),
        sa.Column("workspace_kind", sa.String(64), nullable=False),
        sa.Column("workspace_label", sa.String(255), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("end_reason", sa.String(128), nullable=True),
        sa.Column("error_detail", sa.String(512), nullable=True),
        sa.Column("harness_session_id", sa.String(256), nullable=True),
        sa.Column(
            "idle_timeout_seconds", sa.Integer(), nullable=False, server_default="1800"
        ),
        sa.Column("first_prompt", sa.Text(), nullable=True),
        sa.Column("pending_turn", postgresql.JSONB(), nullable=True),
        sa.Column("pending_turn_sent_at", sa.DateTime(), nullable=True),
        sa.Column("active_turn_id", sa.String(128), nullable=True),
        sa.Column("stop_mode", sa.String(16), nullable=True),
        sa.Column("stop_reason", sa.String(64), nullable=True),
        sa.Column("start_sent_at", sa.DateTime(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_runner_remote_sessions_id", "runner_remote_sessions", ["id"])
    op.create_index(
        "ix_runner_remote_sessions_runtime_session_id",
        "runner_remote_sessions",
        ["runtime_session_id"],
    )
    op.create_index(
        "ix_runner_remote_sessions_runner_state",
        "runner_remote_sessions",
        ["runner_id", "state"],
    )
    op.create_index(
        "ix_runner_remote_sessions_account_state",
        "runner_remote_sessions",
        ["account_id", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runner_remote_sessions_account_state", table_name="runner_remote_sessions"
    )
    op.drop_index(
        "ix_runner_remote_sessions_runner_state", table_name="runner_remote_sessions"
    )
    op.drop_index(
        "ix_runner_remote_sessions_runtime_session_id",
        table_name="runner_remote_sessions",
    )
    op.drop_index("ix_runner_remote_sessions_id", table_name="runner_remote_sessions")
    op.drop_table("runner_remote_sessions")
