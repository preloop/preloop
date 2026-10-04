"""Index account-scoped correlated audit and approval lifecycle lookups."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261004_audit_lookup_idx"
down_revision: Union[str, None] = "20261004_artifact_created_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

INDEXES = (
    ("ix_audit_log_account_correlation", "(details ->> 'correlation_id')"),
    ("ix_audit_log_account_approval", "(details ->> 'approval_id')"),
)


def upgrade() -> None:
    """Build partial indexes concurrently, repairing interrupted builds."""
    with op.get_context().autocommit_block():
        for name, expression in INDEXES:
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT 1 FROM pg_index i "
                        "JOIN pg_class c ON c.oid = i.indexrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relname = :name "
                        "AND n.nspname = current_schema() AND NOT i.indisvalid"
                    ),
                    {"name": name},
                )
                .scalar()
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON audit_log (account_id, {expression}) "
                f"WHERE {expression} IS NOT NULL"
            )


def downgrade() -> None:
    """Remove lookup indexes without blocking audit writes."""
    with op.get_context().autocommit_block():
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
