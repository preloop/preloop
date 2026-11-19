"""Queue background sealing of existing execution credential fingerprints."""

from alembic import op

revision = "20261224_seal_exec_fp"
down_revision = "20261223_api_traffic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Register the idempotent batch job without rewriting executions in the hook."""
    config = op.get_context().config
    fresh = bool(config and config.attributes.get("fresh_database"))
    active, parked = ("TRUE", "FALSE") if fresh else ("FALSE", "TRUE")
    op.execute(
        "INSERT INTO agent_usage_backfill(name, ready) VALUES "
        f"('exec_fingerprint_protection', {active}), "
        f"('cred_execution_fingerprints', {parked}) ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    """Remove both registry entries after the operator restores the legacy form."""
    op.execute(
        "DELETE FROM agent_usage_backfill WHERE name IN "
        "('cred_execution_fingerprints', 'exec_fingerprint_protection')"
    )
