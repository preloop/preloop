"""Add ``parent_session_id`` to ``runtime_session`` (subagent lineage).

One nullable, indexed, self-referencing column: the session that spawned this
one, where the harness says so on the wire. Rows that exist before this
revision read back NULL, which is the same value a harness that cannot
distinguish a subagent turn produces, so nothing has to be backfilled and no
consumer can tell an old row from a lineage-less new one.

Column shape (a copy of the ``parent_execution_id`` pattern landed for
``flow_execution``):

- ``parent_session_id`` UUID, NULL, FK -> ``runtime_session.id`` (SET NULL on
  delete), indexed. Deleting a parent leaves the child row intact with an
  unknown parent rather than deleting a conversation nobody asked to delete.

Revision ID: 20260915_session_parent
Revises: 20260915_session_embedding
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260915_session_parent"
down_revision: Union[str, None] = "20260915_session_embedding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the parent column, its self-referencing foreign key and its index."""
    op.add_column(
        "runtime_session",
        sa.Column(
            "parent_session_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_runtime_session_parent_session_id",
        "runtime_session",
        "runtime_session",
        ["parent_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_runtime_session_parent_session_id",
        "runtime_session",
        ["parent_session_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the parent column and everything added for it."""
    op.drop_index(
        "ix_runtime_session_parent_session_id",
        table_name="runtime_session",
    )
    op.drop_constraint(
        "fk_runtime_session_parent_session_id",
        "runtime_session",
        type_="foreignkey",
    )
    op.drop_column("runtime_session", "parent_session_id")
