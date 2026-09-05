"""Add flow_execution.evidence_archive for captured evidence packs.

Revision ID: 20260821_flow_exec_evidence
Revises: 20260818_notify_toggles
Create Date: 2026-08-21

Audit-style flows write an evidence pack under ``/workspace/evidence``;
the runner captures it as a size-capped tar.gz after the agent finishes
(Docker archive API, or the Kubernetes log-channel emission) so
``GET /flows/executions/{id}/evidence`` can serve it after the container
or pod is gone.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260821_flow_exec_evidence"
down_revision: Union[str, None] = "20260818_notify_toggles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable binary evidence_archive column."""
    op.add_column(
        "flow_execution",
        sa.Column("evidence_archive", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    """Drop the evidence_archive column (captured packs are lost)."""
    op.drop_column("flow_execution", "evidence_archive")
