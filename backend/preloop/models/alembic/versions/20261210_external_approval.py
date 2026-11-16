"""Store external approval references and durable notification hand-off.

Revision ID: 20261210_external_approval
Revises: 20261113_bundle_registry
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261210_external_approval"
down_revision = "20261113_bundle_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable fields so existing approval workflows keep their behavior."""
    for name, kind in (
        ("external_reference", sa.String(512)),
        ("external_url", sa.Text()),
        ("external_result", postgresql.JSONB()),
        ("external_handoff_state", sa.String(32)),
        ("correlation_id", sa.String(128)),
        ("server_name", sa.String(255)),
    ):
        op.add_column("approval_request", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    """Remove external hand-off storage."""
    for name in (
        "external_reference",
        "external_url",
        "external_result",
        "external_handoff_state",
        "correlation_id",
        "server_name",
    ):
        op.drop_column("approval_request", name)
