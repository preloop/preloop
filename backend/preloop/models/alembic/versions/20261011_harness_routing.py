"""Add routing_reason and billing_mode to flow executions (harness routing, #1481)."""

import sqlalchemy as sa
from alembic import op

revision = "20261011_harness_routing"
down_revision = "20261010_otlp_telemetry_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "flow_execution",
        sa.Column("routing_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("billing_mode", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("flow_execution", "billing_mode")
    op.drop_column("flow_execution", "routing_reason")
