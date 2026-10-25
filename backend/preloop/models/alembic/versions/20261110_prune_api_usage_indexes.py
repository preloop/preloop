"""Drop redundant and unused api_usage indexes.

Revision ID: 20261110_prune_api_usage_idx
Revises: 20261110_assessment_pr_identity

Every api_usage insert maintains each index on the table. Three carry no
query that another index cannot serve:

- ix_api_usage_id duplicates api_usage_pkey (both are btrees on id).
- ix_api_usage_account_id is a prefix of the account-leading composites
  (ix_api_usage_account_action_ts and others), which serve account-only
  filters and the ON DELETE SET NULL lookup from account.
- ix_api_usage_imported_parent_conv has no reader: nothing filters on
  parent_conversation_id; the rollup groups by conversation_id.

The drops run concurrently under the bounded concurrent-index budget.
"""

from alembic import op
import sqlalchemy as sa

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261110_prune_api_usage_idx"
down_revision = "20261110_assessment_pr_identity"
branch_labels = None
depends_on = None

DEFINITIONS = {
    "ix_api_usage_id": "ON api_usage (id)",
    "ix_api_usage_account_id": "ON api_usage (account_id)",
    "ix_api_usage_imported_parent_conv": (
        "ON api_usage (account_id, parent_conversation_id) "
        "WHERE action_type = 'imported_usage' "
        "AND parent_conversation_id IS NOT NULL"
    ),
}
DROPPED = tuple(DEFINITIONS)


def upgrade() -> None:
    """Drop each index concurrently; absent indexes are skipped."""
    with concurrent_index_ddl(op.get_context()):
        for name in DROPPED:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))


def downgrade() -> None:
    """Rebuild the dropped indexes, replacing any invalid leftover first."""
    with concurrent_index_ddl(op.get_context()):
        for name, definition in DEFINITIONS.items():
            valid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                        "ON c.oid = i.indexrelid WHERE c.relname = :name "
                        "AND c.relnamespace = to_regnamespace(current_schema())"
                    ),
                    {"name": name},
                )
                .scalar()
            )
            if valid is False:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            op.execute(
                sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} {definition}")
            )
