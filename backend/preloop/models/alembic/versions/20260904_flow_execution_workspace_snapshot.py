"""Add flow_execution.workspace_snapshot for captured workspaces.

Revision ID: 20260904_flow_exec_workspace
Revises: 20260904_acct_runner_pool
Create Date: 2026-09-04

Every hosted run now leaves a size-capped tar.gz of ``/workspace`` behind so
an execution that failed before pushing can be restored (and a correlated
resume can continue the unpushed branch). Served by
``GET /flows/executions/{id}/workspace`` and reaped by the workspace janitor
after WORKSPACE_SNAPSHOT_TTL_HOURS.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260904_flow_exec_workspace"
down_revision: Union[str, None] = "20260904_acct_runner_pool"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable binary workspace_snapshot column."""
    op.add_column(
        "flow_execution",
        sa.Column("workspace_snapshot", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    """Drop the workspace_snapshot column (captured workspaces are lost)."""
    op.drop_column("flow_execution", "workspace_snapshot")
