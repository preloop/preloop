"""Add rate-limit telemetry column and index to api_usage.

Revision ID: 20260801_api_usage_rate_limit
Revises: 20260730_webauthn_credentials
Create Date: 2026-08-01

Adds ``api_usage.rate_limit_retry_after_ms`` (nullable): the provider-advised
``Retry-After`` observed on an upstream rate-limited response, in
milliseconds, so "time blocked by rate limits" aggregates without JSONB
scans. The full observed header snapshot lives in
``meta_data["rate_limit"]`` (#136).

Also adds a partial index over 429 rows for the rate-limit report queries.

NOTE: parented on ``20260730_webauthn_credentials`` (main's head at branch
time). Open PRs #137 and #141 add migrations off the same head; whichever
lands last must re-parent.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260801_api_usage_rate_limit"
down_revision = "20260730_webauthn_credentials"
branch_labels = None
depends_on = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the rate_limit_retry_after_ms column and the 429 partial index."""
    op.add_column(
        "api_usage",
        sa.Column("rate_limit_retry_after_ms", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_api_usage_rate_limited",
        "api_usage",
        ["account_id", "timestamp"],
        postgresql_where=sa.text("status_code = 429"),
    )


def downgrade() -> None:
    """Drop the rate-limit telemetry column and index."""
    op.drop_index("ix_api_usage_rate_limited", table_name="api_usage")
    op.drop_column("api_usage", "rate_limit_retry_after_ms")
