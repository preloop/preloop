"""Add flow_execution.cli_session for native CLI session resume.

Revision ID: 20260905_flow_cli_session
Revises: 20260904_flow_notifications
Create Date: 2026-09-05

A PR-comment restart of an issue-implementation flow starts a cold agent
because the OpenCode/Codex session files die with the container. This nullable
JSONB column stores the CLI session id captured from the container log stream
(``{"agent_type": ..., "session_id": ...}``) so the correlated resume can
restore the packed session storage and invoke the CLI resume flag.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "20260905_flow_cli_session"
down_revision: Union[str, None] = "20260904_flow_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable cli_session JSONB column."""
    op.add_column(
        "flow_execution",
        sa.Column("cli_session", JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop the cli_session column (recorded session ids are lost)."""
    op.drop_column("flow_execution", "cli_session")
