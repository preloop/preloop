"""Track execution credential masking fingerprints."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261220_exec_credentials"
down_revision = "20261219_ext_grant_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add an internal, non-recoverable masking inventory."""
    op.add_column(
        "flow_execution",
        sa.Column("credential_fingerprints", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Remove the masking inventory."""
    op.drop_column("flow_execution", "credential_fingerprints")
