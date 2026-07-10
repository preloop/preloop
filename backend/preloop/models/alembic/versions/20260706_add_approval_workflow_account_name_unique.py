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
    # Deduplicate existing names first so the unique index can be created on
    # upgraded deployments: the oldest workflow keeps its name, later
    # duplicates get a stable short-id suffix (e.g. "Deploy approval [3f9a1c2b]").
    # Truncate to 240 chars before the 12-char suffix so the result fits
    # approval_workflow.name (String(255) in models/tool_configuration.py).
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY account_id, name
                       ORDER BY created_at, id
                   ) AS rn
            FROM approval_workflow
        )
        UPDATE approval_workflow aw
        SET name = left(aw.name, 240) || ' [' || left(aw.id::text, 8) || ']'
        FROM ranked r
        WHERE aw.id = r.id
          AND r.rn > 1
        """
    )
    op.create_index(
        "uq_approval_workflow_account_name",
        "approval_workflow",
        ["account_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the per-account approval workflow name uniqueness constraint."""
    op.drop_index("uq_approval_workflow_account_name", table_name="approval_workflow")
