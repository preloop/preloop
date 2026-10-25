"""Restore action types retained before a manual governance usage relabel.

Revision ID: 20261102_restore_usage_action
Revises: 20261102_agent_usage_idx

Deployment cost: upgrade scans api_usage once; runtime scales with table size
and must be measured on a representative copy before scheduling deployment.
PostgreSQL takes a ROW EXCLUSIVE table lock (ordinary reads/writes continue)
and row locks on changed records until the migration commits. The update only
rewrites matching records. No extra index is built for this one-time repair:
that would itself scan the table and add index-build cost. Schedule a maintenance
window if the scan or affected-row lock duration exceeds the deployment budget.
Customer-imported usage is excluded because its metadata is untrusted.
"""

from alembic import op

revision = "20261102_restore_usage_action"
down_revision = "20261102_agent_usage_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Restore only governance rows with retained provenance and mark changes."""
    op.execute("""
        UPDATE api_usage
        SET meta_data = jsonb_set(meta_data, '{report_restore_action_type}',
                                  coalesce(to_jsonb(action_type), 'null'::jsonb)),
            action_type = meta_data ->> 'action_type_before'
        WHERE usage_source IS DISTINCT FROM 'imported'
          AND action_type IS DISTINCT FROM 'imported_usage'
          AND left(meta_data ->> 'purpose', 11) = 'governance_'
          AND nullif(meta_data ->> 'action_type_before', '') IS NOT NULL
          AND action_type IS DISTINCT FROM meta_data ->> 'action_type_before'
          AND NOT (meta_data ? 'report_restore_action_type')
    """)


def downgrade() -> None:
    """Undo only this migration's changes when the restored value is intact."""
    op.execute("""
        UPDATE api_usage
        SET action_type = meta_data ->> 'report_restore_action_type',
            meta_data = meta_data - 'report_restore_action_type'
        WHERE usage_source IS DISTINCT FROM 'imported'
          AND action_type IS DISTINCT FROM 'imported_usage'
          AND meta_data ? 'report_restore_action_type'
          AND action_type = meta_data ->> 'action_type_before'
    """)
