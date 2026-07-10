"""Add composite index for CLI activity DISTINCT ip_address queries.

Revision ID: 20260710_audit_cli_idx
Revises: 20260710_cli_clients
Create Date: 2026-07-10

Supports ``AuditLogCRUD.get_cli_activity_stats``, which filters by
account_id + action + status + timestamp and aggregates DISTINCT
ip_address. Without this covering index the DISTINCT scan can get
expensive as the audit_log table grows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260710_audit_cli_idx"
down_revision: Union[str, None] = "20260710_cli_clients"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_log_account_action_status_ts_ip",
        "audit_log",
        ["account_id", "action", "status", sa.text("timestamp DESC"), "ip_address"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_log_account_action_status_ts_ip",
        table_name="audit_log",
    )
