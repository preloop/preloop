"""Add flow_execution.startup_receipt for the server-asserted startup receipt.

A read-only review on the hosted executor verifies its pinned checkout and
runs setup inside the container before the agent starts. The controller
records that observation here, outside the agent-authored result, so a
restricted CI caller can read it without log access.

Revision ID: 20261115_startup_receipt
Revises: 20261114_owner_flow_subscription
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261115_startup_receipt"
down_revision = "20261114_owner_flow_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable JSONB startup_receipt column."""
    op.add_column(
        "flow_execution",
        sa.Column("startup_receipt", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop the startup_receipt column."""
    op.drop_column("flow_execution", "startup_receipt")
