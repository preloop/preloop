"""add budget notify recipient fields

Revision ID: 20260630_budget_notify
Revises: 20260629_session_title
Create Date: 2026-06-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260630_budget_notify"
down_revision: Union[str, None] = "20260629_session_title"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "budget_policies",
        sa.Column(
            "notification_user_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
    )
    op.add_column(
        "budget_policies",
        sa.Column(
            "notification_team_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("budget_policies", "notification_team_ids")
    op.drop_column("budget_policies", "notification_user_ids")
