"""Index extension identifiers and descriptive text for substring search."""

from alembic import op

revision = "20270103_extension_search"
down_revision = "20270102_extension_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Build trigram indexes for the three fields used by inventory search."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_discovered_extension_id_search "
        "ON discovered_extension USING gin (extension_id gin_trgm_ops)"
    )
    for field in ("name", "description"):
        op.execute(
            f"CREATE INDEX ix_discovered_extension_{field}_search "
            "ON discovered_extension USING gin "
            f"((CAST(details ->> '{field}' AS VARCHAR)) gin_trgm_ops)"
        )


def downgrade() -> None:
    """Remove this feature's indexes while retaining the shared extension."""
    for field in ("id", "name", "description"):
        op.drop_index(f"ix_discovered_extension_{field}_search")
