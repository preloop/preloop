"""Add notifications JSONB to flow.

Revision ID: 20260904_flow_notifications
Revises: 20260904_acct_runner_pool
Create Date: 2026-09-04

Failure and success notifications belong on the flow, not in agent prompts.
A nullable JSONB column keeps existing rows silent (NULL means no comments
and no attention item) and lets each flow opt into commenting on the
triggering issue and raising a console attention item.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "20260904_flow_notifications"
down_revision: Union[str, None] = "20260904_acct_runner_pool"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable per-flow notifications column."""
    op.add_column(
        "flow",
        sa.Column(
            "notifications",
            JSONB(),
            nullable=True,
            comment=(
                "When to comment on the triggering issue and raise a console "
                "attention item after a terminal execution. NULL means none."
            ),
        ),
    )


def downgrade() -> None:
    """Drop the per-flow notifications column."""
    op.drop_column("flow", "notifications")
