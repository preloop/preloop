"""add runtime session title

Revision ID: 20260629_session_title
Revises: 20260626_tool_cost_flag
Create Date: 2026-06-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260629_session_title"
down_revision: Union[str, None] = "20260626_tool_cost_flag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Store a short human-readable title plus its refresh watermark.

    The ``summary``/``summary_updated_at`` columns were added earlier by the
    runtime-session summary migration; this migration only adds the browsable
    title and the request-count watermark used for the periodic refresh.
    """
    op.add_column(
        "runtime_session",
        sa.Column("title", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "runtime_session",
        sa.Column("title_request_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove the runtime-session title columns."""
    op.drop_column("runtime_session", "title_request_count")
    op.drop_column("runtime_session", "title")
