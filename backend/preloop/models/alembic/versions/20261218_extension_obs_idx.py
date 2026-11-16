"""Index extension context history and latest observation coverage reads."""

import sqlalchemy as sa
from alembic import op

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261218_extension_obs_idx"
down_revision = "20261217_extensions_stack"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_discovery_observation_context_evidence",
        "discovery_observation USING gin (evidence jsonb_path_ops)",
    ),
    (
        "ix_discovery_observation_device_latest",
        "discovery_observation (account_id, workstation_fingerprint, ended_at DESC, received_at DESC)",
    ),
)


def upgrade() -> None:
    """Build without blocking reporters; repair an interrupted index build."""
    with concurrent_index_ddl(op.get_context()):
        for name, definition in INDEXES:
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE c.relname=:name AND n.nspname=current_schema() AND NOT i.indisvalid"
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
    """Remove read indexes without changing inventory or approvals."""
    with concurrent_index_ddl(op.get_context()):
        for name, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
