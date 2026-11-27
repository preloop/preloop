"""Allow standalone MCP servers in extension inventory and approvals."""

from alembic import op

revision = "20270102_mcp_extensions"
down_revision = "20270103_native_approval_session"
branch_labels = None
depends_on = None

TABLES = ("discovered_extension", "extension_approval")
OLD_KINDS = "'plugin','skill','hook','command'"


def upgrade() -> None:
    """Extend both kind constraints without changing existing rows."""
    for table in TABLES:
        name = f"{table}_extension_kind_check"
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(
            name, table, f"extension_kind IN ({OLD_KINDS},'mcp_server')"
        )


def downgrade() -> None:
    """Restore old constraints, refusing downgrade while MCP rows remain.

    PostgreSQL rolls back the migration if narrowing would reject existing
    inventory or approvals, preserving those rows and their pins.
    """
    for table in TABLES:
        name = f"{table}_extension_kind_check"
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"extension_kind IN ({OLD_KINDS})")
