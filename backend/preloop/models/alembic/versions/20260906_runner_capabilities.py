"""Store advertised host-execution profile names on flow_runner.

Revision ID: 20260906_runner_caps
Revises: 20260906_flow_feedback
Create Date: 2026-09-06

Private runners advertise named host CLI profiles over register/heartbeat.
The control plane stores names and capability flags only. Executables,
argv, and credentials stay on the runner host.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260906_runner_caps"
down_revision = "20260906_flow_feedback"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add flow_runner.capabilities JSONB."""
    op.add_column(
        "flow_runner",
        sa.Column(
            "capabilities",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Drop flow_runner.capabilities."""
    op.drop_column("flow_runner", "capabilities")
