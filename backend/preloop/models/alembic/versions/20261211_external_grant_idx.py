"""Index external grant searches and pending hand-off notifications concurrently."""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261211_external_grant_idx"
down_revision = "20261210_external_approval"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_audit_log_grant_reference",
        "audit_log (account_id, (details -> 'external_result' ->> 'grant_reference')) "
        "WHERE details -> 'external_result' ->> 'grant_reference' IS NOT NULL",
    ),
    (
        "ix_approval_request_external_notifications",
        "approval_request (id) WHERE external_handoff_state = 'notify'",
    ),
)


def upgrade() -> None:
    """Build lookup indexes without blocking writes, repairing interrupted builds."""
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
    """Remove lookup indexes without blocking audit or approval writes."""
    with concurrent_index_ddl(op.get_context()):
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
