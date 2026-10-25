"""Recording timebases and browser-step server receipt timestamps.

Revision ID: 20261110_media_timing
Revises: 20261111_credential_columns
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261110_media_timing"
down_revision = "20261111_credential_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runtime_session_artifact",
        sa.Column("media_timing", postgresql.JSONB(), nullable=True),
    )
    # created_at is a naive UTC server insertion time, unlike timestamp which
    # may already be the producer's occurred_at. Never infer receipt from it.
    op.execute("""
        UPDATE runtime_session_activity
        SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
            'recorded_at', to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
        WHERE activity_type = 'browser_step'
          AND NOT COALESCE(metadata, '{}'::jsonb) ? 'recorded_at'
    """)


def downgrade() -> None:
    op.drop_column("runtime_session_artifact", "media_timing")
    op.execute("""
        UPDATE runtime_session_activity SET metadata = metadata - 'recorded_at'
        WHERE activity_type = 'browser_step' AND metadata ? 'recorded_at'
    """)
