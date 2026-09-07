"""Add flow_execution.evidence_receipt for availability/retention status.

Revision ID: 20260907_evidence_receipt
Revises: 20260906_lifecycle_key_merge
Create Date: 2026-09-07

Durable evidence lives in flow_artifact. This column stores the public
receipt (digest, size, expiry, available/missing/expired/failed) so a
release consumer can check evidence without downloading the pack. It is
not a legal-hold or object-lock claim.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260907_evidence_receipt"
down_revision: Union[str, None] = "20260906_lifecycle_key_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable JSONB evidence_receipt column."""
    op.add_column(
        "flow_execution",
        sa.Column("evidence_receipt", JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop the evidence receipt column; durable artifacts remain."""
    op.drop_column("flow_execution", "evidence_receipt")
