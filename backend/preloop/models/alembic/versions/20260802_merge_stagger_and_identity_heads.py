"""Merge stagger_email head with the redundant identity merge head.

No-op merge revision. ``20260801_stagger_email`` already merges the three
sibling heads from #132/#150/#151/#152 *and* adds ``stagger_email``. A later
no-op merge (``20260802_merge_heads``) repeated that same three-parent merge,
leaving two Alembic heads and breaking ``alembic upgrade head``.

This revision reunifies the graph without re-parenting either revision, so
environments that applied either path remain valid.

Revision ID: 20260802_merge_stagger_heads
Revises: 20260801_stagger_email, 20260802_merge_heads
Create Date: 2026-08-02

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "20260802_merge_stagger_heads"
down_revision: Union[str, Sequence[str], None] = (
    "20260801_stagger_email",
    "20260802_merge_heads",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Upgrade schema (no-op merge)."""


def downgrade() -> None:
    """Downgrade schema (no-op merge)."""
