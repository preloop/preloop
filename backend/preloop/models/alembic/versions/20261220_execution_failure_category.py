"""Guarantee a named category for failed executions, including legacy writers.

Revision ID: 20261220_execution_failure
Revises: 20261219_ext_grant_merge
"""

from alembic import op

revision = "20261220_execution_failure"
down_revision = "20261219_ext_grant_merge"
branch_labels = None
depends_on = None


# A storage fallback protects bulk updates and workers still running an older
# version. Rich classification remains in the application; storage only names
# unambiguous legacy messages, otherwise the execution write boundary.
CATEGORY_SQL = """
CASE
    WHEN status IN ('TIMEOUT', 'TIMED_OUT') THEN 'timeout'
    WHEN status IN ('STOPPED', 'CANCELLED') THEN 'cancelled'
    WHEN error_message ILIKE '%native_result requires an enabled result_schema%'
        THEN 'flow_config'
    WHEN error_message ILIKE '%explicit failure in result.json%'
        THEN 'agent_reported_failure'
    WHEN error_message ILIKE '%repeated%tool loop:%' THEN 'agent_tool_loop'
    ELSE 'execution_error'
END
"""


def upgrade() -> None:
    """Backfill uncategorized failures and guard all future terminal writes."""
    op.execute(
        f"""UPDATE flow_execution SET failure_category = {CATEGORY_SQL}
        WHERE status IN ('FAILED', 'ERROR', 'TIMEOUT', 'TIMED_OUT', 'ABORTED', 'STOPPED', 'CANCELLED')
          AND (failure_category IS NULL OR failure_category IN ('', 'unknown'))"""
    )
    op.execute("""
        CREATE FUNCTION ensure_flow_failure_category() RETURNS trigger AS $$
        BEGIN
            IF NEW.status IN ('FAILED', 'ERROR', 'TIMEOUT', 'TIMED_OUT', 'ABORTED', 'STOPPED', 'CANCELLED')
               AND (NEW.failure_category IS NULL
                    OR NEW.failure_category IN ('', 'unknown')) THEN
                NEW.failure_category := CASE
                    WHEN NEW.status IN ('TIMEOUT', 'TIMED_OUT') THEN 'timeout'
                    WHEN NEW.status IN ('STOPPED', 'CANCELLED') THEN 'cancelled'
                    WHEN NEW.error_message ILIKE
                        '%native_result requires an enabled result_schema%'
                        THEN 'flow_config'
                    WHEN NEW.error_message ILIKE '%explicit failure in result.json%'
                        THEN 'agent_reported_failure'
                    WHEN NEW.error_message ILIKE '%repeated%tool loop:%'
                        THEN 'agent_tool_loop'
                    ELSE 'execution_error'
                END;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER flow_failure_category_required
        BEFORE INSERT OR UPDATE OF status, failure_category, error_message
        ON flow_execution FOR EACH ROW EXECUTE FUNCTION ensure_flow_failure_category()
    """)


def downgrade() -> None:
    """Remove the guard while retaining the named historical categories."""
    op.execute("DROP TRIGGER flow_failure_category_required ON flow_execution")
    op.execute("DROP FUNCTION ensure_flow_failure_category()")
