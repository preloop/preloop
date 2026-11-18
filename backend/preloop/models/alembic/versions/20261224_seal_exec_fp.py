"""Queue background sealing of existing execution credential fingerprints."""

from alembic import op

revision = "20261224_seal_exec_fp"
down_revision = "20261223_api_traffic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Register the idempotent batch job without rewriting executions in the hook."""
    op.execute(
        "INSERT INTO agent_usage_backfill(name) "
        "VALUES ('cred_execution_fingerprints') ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    """Remove the scheduler entry, preserving credentials already sealed."""
    op.execute(
        "DELETE FROM agent_usage_backfill WHERE name = 'cred_execution_fingerprints'"
    )
