"""Deduplicate budget spend buckets and enforce NULLS NOT DISTINCT.

Revision ID: 20260712_budget_nnd
Revises: 20260712_agent_control_cmd
Create Date: 2026-07-12

Requires Postgres >= 15 (``UNIQUE NULLS NOT DISTINCT``); the whole stack
ships pgvector:pg16 and CI runs pg16.

The upsert in ``crud_budget_spend.upsert_spend`` relies on ON CONFLICT
against the bucket unique constraint, but Postgres unique constraints treat
NULLs as distinct by default — so account-level buckets
(``subject_id IS NULL``) never conflicted and every gateway request INSERTED
a new row instead of accumulating (observed: ~69k rows for ~4.5k logical
buckets). Spend READS sum the rows, so totals stayed correct; this migration
stops the unbounded row growth:

1. normalize legacy ``model_alias`` NULLs to ``''`` (newer code always
   writes ``''``),
2. collapse duplicate bucket rows into one (summing spend),
3. recreate the unique constraint WITH ``NULLS NOT DISTINCT`` so the
   upsert's ON CONFLICT fires for NULL subject_id / period_start.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260712_budget_nnd"
down_revision: Union[str, None] = "20260712_agent_control_cmd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


BUCKET_COLS = "account_id, subject_type, subject_id, model_alias, period, period_start"


def upgrade() -> None:
    """Dedupe spend buckets and make the unique constraint NULL-safe."""
    op.execute(
        "UPDATE budget_spend_activities SET model_alias = '' WHERE model_alias IS NULL"
    )
    # Fold each logical bucket's total into its first row...
    op.execute(
        f"""
        WITH ranked AS (
            SELECT id,
                   sum(spend_usd) OVER (PARTITION BY {BUCKET_COLS}) AS total,
                   row_number() OVER (
                       PARTITION BY {BUCKET_COLS} ORDER BY id
                   ) AS rn
            FROM budget_spend_activities
        )
        UPDATE budget_spend_activities b
        SET spend_usd = ranked.total
        FROM ranked
        WHERE b.id = ranked.id AND ranked.rn = 1
        """
    )
    # ...and drop the rest.
    op.execute(
        f"""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY {BUCKET_COLS} ORDER BY id
                   ) AS rn
            FROM budget_spend_activities
        )
        DELETE FROM budget_spend_activities b
        USING ranked
        WHERE b.id = ranked.id AND ranked.rn > 1
        """
    )
    op.execute(
        "ALTER TABLE budget_spend_activities "
        "DROP CONSTRAINT uq_budget_spend_activities_period_start"
    )
    op.execute(
        "ALTER TABLE budget_spend_activities "
        "ADD CONSTRAINT uq_budget_spend_activities_period_start "
        f"UNIQUE NULLS NOT DISTINCT ({BUCKET_COLS})"
    )


def downgrade() -> None:
    """Restore the NULLs-distinct unique constraint (data stays deduped)."""
    op.execute(
        "ALTER TABLE budget_spend_activities "
        "DROP CONSTRAINT uq_budget_spend_activities_period_start"
    )
    op.execute(
        "ALTER TABLE budget_spend_activities "
        "ADD CONSTRAINT uq_budget_spend_activities_period_start "
        f"UNIQUE ({BUCKET_COLS})"
    )
