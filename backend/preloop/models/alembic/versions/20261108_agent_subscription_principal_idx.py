"""Index subscription gateway usage by runtime principal for the agents list.

Revision ID: 20261108_agent_sub_principal
Revises: 20261107_gov_capture_retry

The account-wide subscription index serves Spend but cannot bound each
agent's LATERAL aggregate. Keep it and add a sparse principal index so that
one agent's subscription total does not scan every gateway request it made.
"""

from alembic import op
import sqlalchemy as sa

from preloop.models.concurrent_index_ddl import concurrent_index_ddl

revision = "20261108_agent_sub_principal"
down_revision = "20261107_gov_capture_retry"
branch_labels = None
depends_on = None

NAME = "ix_api_usage_principal_subscription"
DEFINITION = (
    "ON api_usage (account_id, runtime_principal_type, "
    "runtime_principal_id, timestamp) "
    "WHERE action_type = 'model_gateway' AND cost_source = 'subscription'"
)


def upgrade() -> None:
    """Build the principal index concurrently and repair an invalid build."""
    with concurrent_index_ddl(op.get_context()):
        valid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                    "ON c.oid = i.indexrelid WHERE c.relname = :name "
                    "AND c.relnamespace = to_regnamespace(current_schema())"
                ),
                {"name": NAME},
            )
            .scalar()
        )
        if valid is False:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}"))
        op.execute(
            sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME} {DEFINITION}")
        )


def downgrade() -> None:
    """Drop only the per-principal index; retain the Spend index."""
    with concurrent_index_ddl(op.get_context()):
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {NAME}"))
