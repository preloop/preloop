"""Which read-back comparison failed for a platform-posted review.

Revision ID: 20261225_review_readback
Revises: 20261224_seal_exec_fp

The publication row already stores the review id and the status id. This
column stores only the machine name of the comparison that failed
(``review_absent``, ``review_body``, ``status_target_url``, and the others).
It holds no provider content. Existing rows stay null.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261225_review_readback"
down_revision: Union[str, None] = "20261224_seal_exec_fp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the read-back check and the sent body digest and status URL.

    Existing rows stay null. A later verification of those rows compares
    a fresh render, and a difference stays failed.
    """
    op.add_column(
        "platform_review_publication",
        sa.Column("readback_check", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "platform_review_publication",
        sa.Column("body_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "platform_review_publication",
        sa.Column("status_target_url", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    """Drop the read-back columns. Publication rows stay."""
    op.drop_column("platform_review_publication", "status_target_url")
    op.drop_column("platform_review_publication", "body_digest")
    op.drop_column("platform_review_publication", "readback_check")
