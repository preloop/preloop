"""add unique approval workflow name per account

Revision ID: 20260706_aw_name_uq
Revises: 9a5b2c3d4e9q
Create Date: 2026-07-06
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260706_aw_name_uq"
down_revision: Union[str, None] = "20260705_replay_run"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Prevent duplicate approval workflow names within one account."""
    op.create_index(
        "uq_approval_workflow_account_name",
        "approval_workflow",
        ["account_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the per-account approval workflow name uniqueness constraint."""
    op.drop_index("uq_approval_workflow_account_name", table_name="approval_workflow")
