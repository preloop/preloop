"""Persist immutable CI review correlation without backfilling history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261004_ci_execution_binding"
down_revision = "20261003_discovery_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable snapshots and prevent changing accepted attribution."""
    op.add_column(
        "flow_execution", sa.Column("ci_review_binding", JSONB(), nullable=True)
    )
    op.execute("""
        CREATE FUNCTION protect_ci_execution_binding() RETURNS trigger AS $$
        BEGIN
            IF NEW.ci_principal_id IS DISTINCT FROM OLD.ci_principal_id
                OR NEW.ci_review_binding IS DISTINCT FROM OLD.ci_review_binding
                OR (OLD.ci_review_binding IS NOT NULL
                    AND NEW.flow_id IS DISTINCT FROM OLD.flow_id)
                OR (NEW.initiating_ci_key_id IS NOT NULL
                    AND NEW.initiating_ci_key_id IS DISTINCT FROM OLD.initiating_ci_key_id)
            THEN
                RAISE EXCEPTION 'CI execution attribution is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER protect_ci_execution_binding
        BEFORE UPDATE ON flow_execution FOR EACH ROW
        EXECUTE FUNCTION protect_ci_execution_binding();
    """)


def downgrade() -> None:
    """Never erase an accepted execution's correlation snapshot."""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM flow_execution WHERE ci_review_binding IS NOT NULL)
            THEN RAISE EXCEPTION 'Cannot erase CI execution correlation';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER protect_ci_execution_binding ON flow_execution")
    op.execute("DROP FUNCTION protect_ci_execution_binding()")
    op.drop_column("flow_execution", "ci_review_binding")
