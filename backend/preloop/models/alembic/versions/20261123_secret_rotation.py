"""Add rotation metadata to secret references.

Revision ID: 20261123_secret_rotation
Revises: 20261121_exception_digest

Additive and safe during a rolling upgrade: ``version`` has a server default
of 1, so existing rows and writers of the previous release keep working, and
``rotated_at`` is nullable (None means never rotated). Adding a column with a
constant default does not rewrite the table in PostgreSQL 11+. Downgrade drops
both columns; no credential material is touched in either direction.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261123_secret_rotation"
down_revision = "20261121_exception_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the rotation counter and timestamp."""
    op.add_column(
        "secret_reference",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "secret_reference",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the rotation metadata; secret material is unaffected."""
    op.drop_column("secret_reference", "rotated_at")
    op.drop_column("secret_reference", "version")
