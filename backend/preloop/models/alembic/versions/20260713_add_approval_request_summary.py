"""Add human-readable summary column to approval_request.

Revision ID: 20260713_approval_summary
Revises: 20260712_flow_orch_claim
Create Date: 2026-07-13

Stores an LLM- or ask_user-generated plain-language ask shown first across
console, mobile, watch, push, and Slack/Mattermost.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260713_approval_summary"
down_revision: Union[str, None] = "20260712_flow_orch_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable summary column."""
    op.add_column(
        "approval_request",
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="User-facing plain-language ask for this approval",
        ),
    )


def downgrade() -> None:
    """Drop summary column."""
    op.drop_column("approval_request", "summary")
