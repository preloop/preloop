"""Add fx_rate_to_usd to model_price_overrides.

Revision ID: 20260712_override_fx
Revises: 20260712_usage_accuracy
Create Date: 2026-07-12

Non-USD overrides previously stored a currency code that no cost math ever
read, silently mis-recording e.g. EUR contract prices as USD. The explicit
FX rate lets ``to_pricing_dict`` convert to USD at write time.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260712_override_fx"
down_revision: Union[str, None] = "20260712_usage_accuracy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add fx_rate_to_usd column."""
    op.add_column(
        "model_price_overrides",
        sa.Column("fx_rate_to_usd", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Drop fx_rate_to_usd column."""
    op.drop_column("model_price_overrides", "fx_rate_to_usd")
