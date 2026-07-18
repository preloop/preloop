"""add managed-agent identity columns to approval_request

Adds nullable managed_agent_id, runtime_session_id, and managed_agent_name to
approval_request so approvals raised for onboarded-agent native tool calls can
identify which agent is asking on operator surfaces (mobile/watch). Nullable
and un-constrained to preserve flow/MCP-originated approvals.

Revision ID: 20260630_approval_agent_id
Revises: 20260630_tool_output_filter
Create Date: 2026-06-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260630_approval_agent_id"
down_revision: Union[str, None] = "20260630_budget_notify"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add managed-agent identity columns to approval_request."""
    op.add_column(
        "approval_request",
        sa.Column("managed_agent_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "approval_request",
        sa.Column("runtime_session_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "approval_request",
        sa.Column("managed_agent_name", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_approval_request_managed_agent_id",
        "approval_request",
        ["managed_agent_id"],
    )
    op.create_index(
        "ix_approval_request_runtime_session_id",
        "approval_request",
        ["runtime_session_id"],
    )


def downgrade() -> None:
    """Drop the managed-agent identity columns."""
    op.drop_index(
        "ix_approval_request_runtime_session_id", table_name="approval_request"
    )
    op.drop_index("ix_approval_request_managed_agent_id", table_name="approval_request")
    op.drop_column("approval_request", "managed_agent_name")
    op.drop_column("approval_request", "runtime_session_id")
    op.drop_column("approval_request", "managed_agent_id")
