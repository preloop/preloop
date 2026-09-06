"""Advertise trusted private publication helper readiness.

Revision ID: 20260906_publication_caps
Revises: 20260906_flow_artifacts
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260906_publication_caps"
down_revision = "20260906_flow_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Existing runners are incapable until a ready heartbeat arrives."""
    op.add_column(
        "flow_runner",
        sa.Column(
            "publication_capabilities",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Remove private publication capability metadata."""
    op.drop_column("flow_runner", "publication_capabilities")
