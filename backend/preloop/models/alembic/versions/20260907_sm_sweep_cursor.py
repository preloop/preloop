"""Per-tenant rotating keyset for bounded security-maintenance sweeps.

Revision ID: 20260907_sm_sweep_cursor
Revises: 20260907_sm_pending_item
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_sm_sweep_cursor"
down_revision: Union[str, None] = "20260907_sm_pending_item"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Store the last visited item and baseline ids for the next sweep."""
    op.create_table(
        "security_maintenance_sweep",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_after_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("baseline_after_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("account_id", name="uq_sm_sweep_account"),
    )
    op.create_index(
        "ix_security_maintenance_sweep_id",
        "security_maintenance_sweep",
        ["id"],
    )
    op.create_index(
        "ix_security_maintenance_sweep_account_id",
        "security_maintenance_sweep",
        ["account_id"],
    )


def downgrade() -> None:
    """Drop the rotating sweep cursor table."""
    op.drop_index(
        "ix_security_maintenance_sweep_account_id",
        table_name="security_maintenance_sweep",
    )
    op.drop_index(
        "ix_security_maintenance_sweep_id",
        table_name="security_maintenance_sweep",
    )
    op.drop_table("security_maintenance_sweep")
