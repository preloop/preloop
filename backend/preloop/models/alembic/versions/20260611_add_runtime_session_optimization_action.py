"""add runtime session optimization action table

Revision ID: 20260611_session_opt_action
Revises: 20260610_session_opt_cache
Create Date: 2026-06-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260611_session_opt_action"
down_revision: Union[str, None] = "20260610_session_opt_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the applied optimization action table."""
    op.create_table(
        "runtime_session_optimization_action",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, index=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "runtime_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runtime_session.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("suggestion_id", sa.String(80), nullable=False),
        sa.Column("suggestion_title", sa.String(160), nullable=True),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("applied_by", sa.String(255), nullable=True),
        sa.Column("runtime_principal_id", sa.String(255), nullable=True),
        sa.Column("baseline", JSONB(), nullable=True),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Drop the applied optimization action table."""
    op.drop_table("runtime_session_optimization_action")
