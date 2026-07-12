"""Add composite indexes for high-volume usage/session/approval queries.

Revision ID: 20260712_usage_perf_idx
Revises: 20260712_enum_checks
Create Date: 2026-07-12

Supports account-scoped api_usage aggregations (action_type / principal +
timestamp), runtime_session last-activity listing, and approval_request
status queues. Uses IF NOT EXISTS so re-runs on partially migrated DBs
are safe.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260712_usage_perf_idx"
down_revision: Union[str, None] = "20260712_enum_checks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create composite performance indexes."""
    op.create_index(
        "ix_api_usage_account_action_ts",
        "api_usage",
        ["account_id", "action_type", sa.text("timestamp DESC")],
        if_not_exists=True,
    )
    op.create_index(
        "ix_api_usage_account_principal_ts",
        "api_usage",
        [
            "account_id",
            "runtime_principal_type",
            "runtime_principal_id",
            sa.text("timestamp DESC"),
        ],
        if_not_exists=True,
    )
    op.create_index(
        "ix_runtime_session_account_last_activity",
        "runtime_session",
        ["account_id", sa.text("last_activity_at DESC")],
        if_not_exists=True,
    )
    op.create_index(
        "ix_approval_request_account_status_requested",
        "approval_request",
        ["account_id", "status", sa.text("requested_at DESC")],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop composite performance indexes."""
    op.drop_index(
        "ix_approval_request_account_status_requested",
        table_name="approval_request",
        if_exists=True,
    )
    op.drop_index(
        "ix_runtime_session_account_last_activity",
        table_name="runtime_session",
        if_exists=True,
    )
    op.drop_index(
        "ix_api_usage_account_principal_ts",
        table_name="api_usage",
        if_exists=True,
    )
    op.drop_index(
        "ix_api_usage_account_action_ts",
        table_name="api_usage",
        if_exists=True,
    )
