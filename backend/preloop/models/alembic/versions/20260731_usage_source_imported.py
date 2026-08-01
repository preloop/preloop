"""Allow 'imported' in api_usage cost_source / usage_source markers.

Revision ID: 20260731_usage_imported
Revises: 20260730_webauthn_credentials
Create Date: 2026-07-31

Usage ingest (issue #123) records spend observed outside the model gateway
(e.g. Cursor bundled-model usage imported from the Cursor dashboard CSV or
pushed through the ingest API). Those rows are labeled
``usage_source='imported'`` / ``cost_source='imported'`` so gateway-metered
and imported spend can never be silently mixed.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260731_usage_imported"
down_revision: Union[str, None] = "20260730_webauthn_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Extend the marker CHECK constraints with the 'imported' value."""
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.drop_constraint("ck_api_usage_usage_source", "api_usage", type_="check")
    op.create_check_constraint(
        "ck_api_usage_cost_source",
        "api_usage",
        "cost_source IS NULL OR cost_source IN "
        "('override', 'model_config', 'catalog', 'subscription', 'unpriced', "
        "'imported')",
    )
    op.create_check_constraint(
        "ck_api_usage_usage_source",
        "api_usage",
        "usage_source IS NULL OR usage_source IN "
        "('provider', 'estimated', 'partial', 'imported')",
    )


def downgrade() -> None:
    """Restore the previous marker CHECK constraints (clearing 'imported')."""
    op.execute("UPDATE api_usage SET cost_source = NULL WHERE cost_source = 'imported'")
    op.execute(
        "UPDATE api_usage SET usage_source = NULL WHERE usage_source = 'imported'"
    )
    op.drop_constraint("ck_api_usage_cost_source", "api_usage", type_="check")
    op.drop_constraint("ck_api_usage_usage_source", "api_usage", type_="check")
    op.create_check_constraint(
        "ck_api_usage_cost_source",
        "api_usage",
        "cost_source IS NULL OR cost_source IN "
        "('override', 'model_config', 'catalog', 'subscription', 'unpriced')",
    )
    op.create_check_constraint(
        "ck_api_usage_usage_source",
        "api_usage",
        "usage_source IS NULL OR usage_source IN ('provider', 'estimated', 'partial')",
    )
