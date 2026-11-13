"""Record when a runner-hosted session's runner went away (#1509).

Revision ID: 20261206_session_away_since
Revises: 20261205_ci_dispatch_sink

``runner_remote_sessions.away_since`` is set when a session moves to
``runner_away`` and cleared when it resumes. The resume deadline is
``away_since + idle_timeout_seconds``; storing it keeps the deadline after
the runner reconnects, when live presence no longer says when it left.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261206_session_away_since"
down_revision: Union[str, None] = "20261205_ci_dispatch_sink"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable away timestamp."""
    op.add_column(
        "runner_remote_sessions",
        sa.Column("away_since", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Drop the away timestamp."""
    op.drop_column("runner_remote_sessions", "away_since")
