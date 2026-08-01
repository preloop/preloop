"""Add error_class column to api_usage for upstream failure taxonomy.

Revision ID: 20260801_api_usage_error_class
Revises: 20260730_webauthn_credentials
Create Date: 2026-08-01

Nullable string column so gateway usage rows can distinguish provider-side
failures (network, upstream_overloaded, upstream_rate_limited,
upstream_quota_exhausted, upstream_auth, upstream_disconnect,
client_cancelled) from product failures. Legacy rows stay NULL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260801_api_usage_error_class"
down_revision: Union[str, None] = "20260730_webauthn_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add nullable error_class to api_usage."""
    op.add_column(
        "api_usage", sa.Column("error_class", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Remove error_class from api_usage."""
    op.drop_column("api_usage", "error_class")
