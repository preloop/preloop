"""Store the validated form answer for a structured question.

Revision ID: 20260908_structured_answer
Revises: 20260907_sm_sweep_cursor
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_structured_answer"
down_revision: Union[str, None] = "20260907_sm_sweep_cursor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add approval_request.structured_answer (nullable, additive)."""
    op.add_column(
        "approval_request",
        sa.Column(
            "structured_answer",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=("Validated form answer for a structured question (input_schema)"),
        ),
    )


def downgrade() -> None:
    """Drop the column; every other approval column is untouched."""
    op.drop_column("approval_request", "structured_answer")
