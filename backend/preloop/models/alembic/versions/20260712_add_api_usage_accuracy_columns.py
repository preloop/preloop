"""Add token/cost accuracy columns to api_usage.

Revision ID: 20260712_usage_accuracy
Revises: 20260710_audit_cli_idx
Create Date: 2026-07-12

Adds first-class cache/reasoning token counts, currency, and provenance
markers (cost_source, usage_source, is_retry) so cost analytics no longer
depend on meta_data JSON blobs. All columns are nullable; legacy rows read
as 0 tokens / USD / unknown provenance.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260712_usage_accuracy"
down_revision: Union[str, None] = "20260710_audit_cli_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add accuracy columns to api_usage."""
    op.add_column(
        "api_usage", sa.Column("cache_read_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "api_usage", sa.Column("cache_creation_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "api_usage", sa.Column("reasoning_tokens", sa.Integer(), nullable=True)
    )
    op.add_column("api_usage", sa.Column("currency", sa.String(3), nullable=True))
    op.add_column("api_usage", sa.Column("cost_source", sa.String(32), nullable=True))
    op.add_column("api_usage", sa.Column("usage_source", sa.String(16), nullable=True))
    op.add_column("api_usage", sa.Column("is_retry", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Remove accuracy columns from api_usage."""
    op.drop_column("api_usage", "is_retry")
    op.drop_column("api_usage", "usage_source")
    op.drop_column("api_usage", "cost_source")
    op.drop_column("api_usage", "currency")
    op.drop_column("api_usage", "reasoning_tokens")
    op.drop_column("api_usage", "cache_creation_tokens")
    op.drop_column("api_usage", "cache_read_tokens")
