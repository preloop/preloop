"""Add runtime_session_activity.metadata_bytes.

Readers measured gateway call payloads with ``octet_length(metadata::text)``,
which detoasts and serialises every value. The size is now stored on write.
Schema only: existing rows are filled by the bounded gateway activity
maintenance pass after the deploy, never inside this migration.

Revision ID: 20261112_activity_meta_bytes
Revises: 20261111_credential_columns
"""

from alembic import op
import sqlalchemy as sa

revision = "20261112_activity_meta_bytes"
down_revision = "20261111_credential_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable column; a metadata-only change, no table rewrite."""
    op.add_column(
        "runtime_session_activity",
        sa.Column("metadata_bytes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop the column."""
    op.drop_column("runtime_session_activity", "metadata_bytes")
