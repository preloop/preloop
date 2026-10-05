"""Immutable machine callback ownership; historical endpoints stay human."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261004_ci_subscription_binding"
down_revision = "20261004_ci_exec_artifact_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable snapshot and freeze machine marker/filter attribution."""
    op.add_column(
        "webhook_endpoint", sa.Column("ci_subscription_binding", JSONB(), nullable=True)
    )
    op.execute("""
        CREATE FUNCTION protect_ci_subscription_binding() RETURNS trigger AS $$
        BEGIN
            IF NEW.ci_principal_id IS DISTINCT FROM OLD.ci_principal_id
                OR NEW.ci_subscription_binding IS DISTINCT FROM OLD.ci_subscription_binding
                OR (NEW.initiating_ci_key_id IS NOT NULL
                    AND NEW.initiating_ci_key_id IS DISTINCT FROM OLD.initiating_ci_key_id)
                OR (OLD.initiating_ci_key_id IS NOT NULL
                    AND NEW.initiating_ci_key_id IS NULL
                    AND NEW.ci_principal_id IS NULL
                    AND NEW.ci_subscription_binding IS NULL)
                OR ((OLD.ci_principal_id IS NOT NULL OR OLD.initiating_ci_key_id IS NOT NULL
                    OR OLD.ci_subscription_binding IS NOT NULL) AND (
                    NEW.account_id IS DISTINCT FROM OLD.account_id
                    OR NEW.event_types IS DISTINCT FROM OLD.event_types
                    OR NEW.source IS DISTINCT FROM OLD.source
                    OR NEW.approval_workflow_id IS DISTINCT FROM OLD.approval_workflow_id
                    OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id))
            THEN RAISE EXCEPTION 'CI subscription attribution is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER protect_ci_subscription_binding BEFORE UPDATE ON webhook_endpoint
        FOR EACH ROW EXECUTE FUNCTION protect_ci_subscription_binding();
    """)


def downgrade() -> None:
    """Never erase retained machine subscription attribution."""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM webhook_endpoint WHERE ci_subscription_binding IS NOT NULL)
            THEN RAISE EXCEPTION 'Cannot erase CI subscription correlation'; END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER protect_ci_subscription_binding ON webhook_endpoint")
    op.execute("DROP FUNCTION protect_ci_subscription_binding()")
    op.drop_column("webhook_endpoint", "ci_subscription_binding")
