"""Store blocking review instructions on a flow.

Repositories that cannot commit ``.preloop/review-policy.md`` put the same
text on the flow. The Pull Request Reviewer injects it as
``{{flow.review_instructions}}``. Null means the repository file is the
only source. Existing rows stay null.

Revision ID: 20260927_review_instructions
Revises: 20260927_exec_log_type_ts
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_review_instructions"
down_revision: Union[str, None] = "20260927_exec_log_type_ts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add flow.review_instructions."""
    op.add_column(
        "flow",
        sa.Column(
            "review_instructions",
            sa.Text(),
            nullable=True,
            comment="Blocking review rules injected into the reviewer prompt",
        ),
    )


def downgrade() -> None:
    """Drop flow.review_instructions."""
    op.drop_column("flow", "review_instructions")
