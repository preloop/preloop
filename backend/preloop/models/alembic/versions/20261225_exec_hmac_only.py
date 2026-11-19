"""Admit keyed-only writers after existing readers have rolled forward."""

from alembic import op

revision = "20261225_exec_hmac_only"
down_revision = "20261224_seal_exec_fp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Stop the pre-deploy migration if an old reader may still receive work."""
    op.execute(
        """
        DO $guard$
        BEGIN
            IF NOT COALESCE((SELECT ready FROM agent_usage_backfill
                WHERE name = 'exec_fingerprint_protection' FOR UPDATE), FALSE) THEN
                RAISE EXCEPTION 'Roll out dual-reader build 760d09c1c to all readers, then run --enable-execution-fingerprints before upgrading';
            END IF;
            INSERT INTO agent_usage_backfill(name, ready) VALUES ('exec_hmac_writers', TRUE)
                ON CONFLICT DO NOTHING;
        END
        $guard$
        """
    )


def downgrade() -> None:
    """Allow rollback to the dual reader, which can mask HMAC-only entries."""
    op.execute("DELETE FROM agent_usage_backfill WHERE name = 'exec_hmac_writers'")
