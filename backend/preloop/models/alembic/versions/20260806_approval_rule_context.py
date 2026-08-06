"""add rule_context to approval_request

Carries the matched ToolAccessRule (id, name, condition expression, priority)
onto the approval request so an approver can see WHY the call was gated, not
just which tool was called with which arguments. Nullable: approvals raised
without rule evaluation (the request_approval builtin) and every pre-existing
row legitimately have no rule context, and surfaces omit the block rather than
fabricate one.

Revision ID: 20260806_approval_rule_ctx
Revises: 20260806_ai_model_updated_at
Create Date: 2026-08-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260806_approval_rule_ctx"
down_revision: Union[str, None] = "20260806_ai_model_updated_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable rule_context JSONB column."""
    op.add_column(
        "approval_request",
        sa.Column("rule_context", JSONB, nullable=True),
    )


def downgrade() -> None:
    """Drop the rule_context column."""
    op.drop_column("approval_request", "rule_context")
