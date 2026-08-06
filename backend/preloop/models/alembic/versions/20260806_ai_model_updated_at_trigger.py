"""Add a DB trigger that bumps ai_model.updated_at on every UPDATE.

Revision ID: 20260806_ai_model_updated_at
Revises: 20260801_stagger_email
Create Date: 2026-08-06

The ORM sets updated_at via SQLAlchemy's onupdate=func.now(), but that only
fires for ORM-issued UPDATEs. Hand-run SQL (the demonstrated failure mode:
the 2026-08-06 production default-model flip left updated_at reading
2026-08-05, a day stale) bypasses it entirely, which makes updated_at
useless as an audit signal exactly when it matters most. A BEFORE UPDATE
trigger covers every write path: ORM, raw SQL in code, and manual psql.

The trigger only fires when the row actually changes (WHEN OLD IS DISTINCT
FROM NEW), so no-op UPDATEs do not rewrite audit timestamps. It uses
clock_timestamp() rather than now() because now() is frozen at transaction
start: an UPDATE issued later inside a long transaction would get a stale
stamp, and the trigger would be unobservable inside transaction-wrapped
tests.

Scoped to ai_model only. Every other table shares the same ORM-only gap via
the Base model; extending coverage is a deliberate follow-up, not a blanket
change here (see 2026-08-06 aux-openrouter-timeout report).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260806_ai_model_updated_at"
down_revision: Union[str, None] = "20260801_stagger_email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the shared trigger function and attach it to ai_model."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION preloop_set_updated_at()
        RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = clock_timestamp();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ai_model_set_updated_at
        BEFORE UPDATE ON ai_model
        FOR EACH ROW
        WHEN (OLD.* IS DISTINCT FROM NEW.*)
        EXECUTE FUNCTION preloop_set_updated_at();
        """
    )


def downgrade() -> None:
    """Drop the ai_model trigger and the shared function."""
    op.execute("DROP TRIGGER IF EXISTS trg_ai_model_set_updated_at ON ai_model")
    op.execute("DROP FUNCTION IF EXISTS preloop_set_updated_at()")
