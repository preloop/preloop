"""Merge duplicate issue rows and make (project_id, external_id) unique.

Revision ID: 20261003_issue_extid_unique
Revises: 20261002_runtime_session_cwd
Create Date: 2026-10-03

Concurrent webhook deliveries for one provider issue could each insert a row
(#1194). This migration keeps the oldest row per ``(project_id, external_id)``,
re-points every foreign key that references ``issue.id`` to it, deletes the
extra rows and adds ``uq_issue_project_external_id``.

The foreign keys are read from ``pg_constraint`` at upgrade time so columns
added by plugins are covered too. In core at this revision they are:
``issue.parent_id``, ``comment.issue_id``, ``issueembedding.issue_id``,
``issue_relationship.source_issue_id`` / ``target_issue_id``,
``issue_duplicate`` (four issue columns), ``issue_lifecycle.issue_id``,
``issue_compliance_result.issue_id``, ``issue_cost.issue_id`` and the
security maintenance issue link.

A dependent row that cannot move because the keeper already has an equivalent
row (unique or check violation) belongs to a duplicate and is deleted.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20261003_issue_extid_unique"
down_revision = "20261002_runtime_session_cwd"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

MERGE_DUPLICATES_SQL = """
DO $$
DECLARE
    pair record;
    fk record;
    dep record;
BEGIN
    DROP TABLE IF EXISTS _issue_merge;
    CREATE TEMP TABLE _issue_merge ON COMMIT DROP AS
    SELECT id AS duplicate_id, keeper_id
    FROM (
        SELECT id,
               first_value(id) OVER (
                   PARTITION BY project_id, external_id
                   ORDER BY created_at, id
               ) AS keeper_id
        FROM issue
    ) ranked
    WHERE id <> keeper_id;

    IF NOT EXISTS (SELECT 1 FROM _issue_merge) THEN
        RETURN;
    END IF;

    FOR fk IN
        SELECT c.conrelid::regclass AS tbl, a.attname AS col
        FROM pg_constraint c
        JOIN pg_attribute a
          ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        WHERE c.contype = 'f'
          AND c.confrelid = 'issue'::regclass
          AND array_length(c.conkey, 1) = 1
    LOOP
        FOR pair IN SELECT duplicate_id, keeper_id FROM _issue_merge LOOP
            FOR dep IN EXECUTE format(
                'SELECT ctid FROM %s WHERE %I = $1', fk.tbl, fk.col
            ) USING pair.duplicate_id
            LOOP
                BEGIN
                    EXECUTE format(
                        'UPDATE %s SET %I = $1 WHERE ctid = $2', fk.tbl, fk.col
                    ) USING pair.keeper_id, dep.ctid;
                EXCEPTION WHEN unique_violation OR check_violation THEN
                    EXECUTE format('DELETE FROM %s WHERE ctid = $1', fk.tbl)
                    USING dep.ctid;
                END;
            END LOOP;
        END LOOP;
    END LOOP;

    DELETE FROM issue WHERE id IN (SELECT duplicate_id FROM _issue_merge);
END $$;
"""


def upgrade() -> None:
    """Merge duplicates, then add the unique constraint."""
    op.execute(MERGE_DUPLICATES_SQL)
    op.create_unique_constraint(
        "uq_issue_project_external_id", "issue", ["project_id", "external_id"]
    )


def downgrade() -> None:
    """Drop the constraint. Merged rows are not restored."""
    op.drop_constraint("uq_issue_project_external_id", "issue", type_="unique")
