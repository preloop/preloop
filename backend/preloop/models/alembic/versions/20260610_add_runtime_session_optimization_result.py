"""add runtime session optimization result cache

Revision ID: 20260610_session_opt_cache
Revises: 20260409_session_summary
Create Date: 2026-06-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "20260610_session_opt_cache"
down_revision: Union[str, None] = "20260409_session_summary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static analysis
# treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the optimization result cache table."""
    op.create_table(
        "runtime_session_optimization_result",
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
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=True),
        sa.Column("response", JSONB(), nullable=False),
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
        sa.UniqueConstraint(
            "runtime_session_id",
            "scope_hash",
            name="uq_runtime_session_optimization_scope",
        ),
    )


def downgrade() -> None:
    """Drop the optimization result cache table."""
    op.drop_table("runtime_session_optimization_result")
