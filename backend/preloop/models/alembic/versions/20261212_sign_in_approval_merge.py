"""Merge sign-in policy and external approval revisions

Revision ID: 20261212_sign_in_approval_merge
Revises: 20261116_sign_in_policy, 20261211_external_grant_idx
Create Date: 2026-11-16 22:50:06.755002

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "20261212_sign_in_approval_merge"
down_revision: tuple[str, str] = (
    "20261116_sign_in_policy",
    "20261211_external_grant_idx",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Join both published branches without changing their schema operations."""
    pass


def downgrade() -> None:
    """Restore the two parent heads without removing either feature."""
    pass
