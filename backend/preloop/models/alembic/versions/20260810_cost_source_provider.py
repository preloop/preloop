"""Allow 'provider' in the api_usage cost_source marker.

Revision ID: 20260810_cost_source_provider
Revises: 20260806_ai_model_updated_at
Create Date: 2026-08-10

The gateway now ingests the cost the upstream itself reports in the response
usage payload (OpenRouter usage accounting: ``usage.cost`` /
``usage.cost_details.upstream_inference_cost``) as the authoritative request
cost. Rows priced from that figure are tagged ``cost_source='provider'`` so
exact provider-ledger costs are distinguishable from catalog estimates.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260810_cost_source_provider"
down_revision: Union[str, None] = "20260806_ai_model_updated_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Extend the cost_source CHECK constraint with 'provider'."""
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.create_check_constraint(
        "ck_api_usage_cost_source",
        "api_usage",
        "cost_source IS NULL OR cost_source IN "
        "('override', 'model_config', 'provider', 'catalog', 'subscription', "
        "'unpriced', 'imported')",
    )


def downgrade() -> None:
    """Restore the previous CHECK constraint (downgrading 'provider' rows).

    Rows priced from the provider ledger keep their cost but fall back to the
    ``catalog`` marker so the constraint can be reinstated without data loss.
    """
    op.execute(
        "UPDATE api_usage SET cost_source = 'catalog' WHERE cost_source = 'provider'"
    )
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.create_check_constraint(
        "ck_api_usage_cost_source",
        "api_usage",
        "cost_source IS NULL OR cost_source IN "
        "('override', 'model_config', 'catalog', 'subscription', 'unpriced', "
        "'imported')",
    )
