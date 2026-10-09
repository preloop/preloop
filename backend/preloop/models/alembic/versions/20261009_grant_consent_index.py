"""Index tenant-scoped delegated consent searches without blocking audit writes."""

import sqlalchemy as sa
from alembic import op

revision = "20261009_grant_consent_index"
down_revision = "20261009_mcp_tool_shadow_prefix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Build the partial consent index concurrently on existing audit evidence."""
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_audit_log_account_grant_consent",
            "audit_log",
            ["account_id", sa.text("(details -> 'grant' ->> 'consent_ref')")],
            postgresql_where=sa.text(
                "(details -> 'grant' ->> 'consent_ref') IS NOT NULL"
            ),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Remove the consent index while retaining the evidence rows."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_audit_log_account_grant_consent",
            table_name="audit_log",
            postgresql_concurrently=True,
        )
