"""Record explicit flow webhook secret rotation times without changing secrets."""

import sqlalchemy as sa
from alembic import op

revision = "20261210_flow_webhook_rotation"
down_revision = "20261113_bundle_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable metadata; existing webhook_config values remain untouched."""
    op.add_column(
        "flow",
        sa.Column(
            "webhook_secret_rotated_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    """Remove rotation metadata while retaining every stored webhook secret."""
    op.drop_column("flow", "webhook_secret_rotated_at")
