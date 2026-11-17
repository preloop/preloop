"""Add opaque grant revisions and index evidence without blocking audit writes."""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261217_grant_evidence_idx"
down_revision = "20261216_tool_policy_merge"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_audit_log_account_last_consent",
        "audit_log (account_id, (details -> 'grant' ->> 'last_known_consent_ref')) "
        "WHERE details -> 'grant' ->> 'last_known_consent_ref' IS NOT NULL",
    ),
    (
        "ix_audit_log_server_consent_history",
        "audit_log (account_id, (details -> 'grant' ->> 'server_id'), "
        "(details -> 'grant' ->> 'server_revision'), timestamp DESC, id DESC) "
        "WHERE details -> 'grant' ->> 'consent_ref' IS NOT NULL",
    ),
    (
        "ix_audit_log_account_error_code",
        "audit_log (account_id, (details ->> 'error_code')) "
        "WHERE details ->> 'error_code' IS NOT NULL",
    ),
)


def upgrade() -> None:
    """Add credential generations and build repairable concurrent audit indexes."""
    op.execute(
        "ALTER TABLE mcp_server ADD COLUMN IF NOT EXISTS grant_revision "
        "uuid NOT NULL DEFAULT gen_random_uuid()"
    )
    with concurrent_index_ddl(op.get_context()):
        for name, definition in INDEXES:
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relname = :name AND n.nspname = current_schema() "
                        "AND NOT i.indisvalid"
                    ),
                    {"name": name},
                )
                .scalar()
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}"
            )


def downgrade() -> None:
    """Remove lookup indexes concurrently and drop credential generations."""
    with concurrent_index_ddl(op.get_context()):
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.execute("ALTER TABLE mcp_server DROP COLUMN IF EXISTS grant_revision")
