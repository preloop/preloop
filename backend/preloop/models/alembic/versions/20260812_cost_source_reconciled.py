"""Allow 'reconciled' in the api_usage cost_source marker.

Revision ID: 20260812_cost_source_reconciled
Revises: 20260810_cost_source_provider
Create Date: 2026-08-12

Historical gateway rows that could not be priced at request time (e.g. the
OpenRouter Auto Router before usage accounting landed) can be backfilled
from the provider's own daily activity ledger, allocated proportionally by
tokens. Those costs are honest approximations, not per-request figures, so
they are tagged ``cost_source='reconciled'`` to stay distinguishable from
exact ``provider`` costs and catalog estimates. Each reconciled row also
carries a ``meta_data["reconciled"]`` marker describing the allocation.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260812_cost_source_reconciled"
down_revision: Union[str, None] = "20260810_cost_source_provider"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Extend the cost_source CHECK constraint with 'reconciled'."""
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.create_check_constraint(
        "ck_api_usage_cost_source",
        "api_usage",
        "cost_source IS NULL OR cost_source IN "
        "('override', 'model_config', 'provider', 'catalog', 'subscription', "
        "'unpriced', 'reconciled', 'imported')",
    )


def downgrade() -> None:
    """Restore the previous CHECK constraint (downgrading 'reconciled' rows).

    Reconciled costs were allocated, not observed per request, and 'unpriced'
    semantically means "no cost could be resolved" — so the downgrade maps
    reconciled rows back to unpriced and clears their estimated cost. This is
    lossless-ish rather than lossless: the allocation itself is dropped from
    the cost columns, but each row keeps its ``meta_data["reconciled"]``
    marker (ledger day, ledger total, allocation timestamp), so re-running
    the backfill after a later upgrade restores the same figures.
    """
    op.execute(
        "UPDATE api_usage SET cost_source = 'unpriced', estimated_cost = NULL "
        "WHERE cost_source = 'reconciled'"
    )
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.create_check_constraint(
        "ck_api_usage_cost_source",
        "api_usage",
        "cost_source IS NULL OR cost_source IN "
        "('override', 'model_config', 'provider', 'catalog', 'subscription', "
        "'unpriced', 'imported')",
    )
