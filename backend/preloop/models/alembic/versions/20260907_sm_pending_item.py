"""One pending security-maintenance approval per work item.

Revision ID: 20260907_sm_pending_item
Revises: 20260907_sm_evidence_merge
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "20260907_sm_pending_item"
down_revision: Union[str, None] = "20260907_sm_evidence_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Cancel duplicate pending rows, then add the partial unique index."""
    op.execute(
        text(
            """
            WITH ranked AS (
                SELECT ar.id,
                       ROW_NUMBER() OVER (
                           PARTITION BY ar.account_id, ar.tool_args->>'item_id'
                           ORDER BY
                               CASE
                                   WHEN smi.approval_request_id = ar.id
                                   THEN 0 ELSE 1
                               END,
                               ar.requested_at ASC,
                               ar.id ASC
                       ) AS rn
                FROM approval_request ar
                LEFT JOIN security_maintenance_item smi
                    ON smi.approval_request_id = ar.id
                WHERE ar.tool_name = 'security_maintenance'
                  AND ar.status = 'pending'
                  AND COALESCE(ar.tool_args->>'item_id', '') <> ''
            )
            UPDATE approval_request
            SET status = 'cancelled'
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """
        )
    )
    op.execute(
        text(
            """
            CREATE UNIQUE INDEX uq_sm_pending_approval_item
            ON approval_request (account_id, (tool_args->>'item_id'))
            WHERE tool_name = 'security_maintenance'
              AND status = 'pending'
              AND COALESCE(tool_args->>'item_id', '') <> ''
            """
        )
    )


def downgrade() -> None:
    """Drop the pending-item unique index."""
    op.execute(text("DROP INDEX IF EXISTS uq_sm_pending_approval_item"))
