"""Store the harness inventory a runner publishes.

Revision ID: 20261011_harness_inventory
Revises: 20261010_otlp_telemetry_ingest
Create Date: 2026-10-11

Issue #1480. Personal runners report the agent harnesses installed on their
host (Copilot CLI, Cursor CLI, Claude Code and others) with login state,
governance and models. Both columns are nullable: a runner that predates the
inventory keeps a null value and the console shows "inventory unknown".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20261011_harness_inventory"
down_revision: Union[str, None] = "20261010_otlp_telemetry_ingest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable inventory columns to flow_runner."""
    op.add_column(
        "flow_runner",
        sa.Column(
            "harness_inventory",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "flow_runner",
        sa.Column(
            "harness_inventory_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the inventory columns."""
    op.drop_column("flow_runner", "harness_inventory_updated_at")
    op.drop_column("flow_runner", "harness_inventory")
