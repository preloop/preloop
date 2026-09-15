"""Say how much of a session an account's embedding opt in covers.

``session_embedding_setting`` gains ``scope``: ``summaries_only``, the
default, or ``full``. The default is the point of the column. Embedding
every chunk is already bounded by the daily cap, but a 1536 wide vector is
about 6 KB, a session of about 40 chunks is about 240 KB of vectors, and
10k sessions are roughly 2.4 GB before the HNSW index is built. A title and
summary are one short chunk per session and carry the meaning a person
searches by, so ``summaries_only`` buys most of the value at about a
fortieth of the storage and the provider spend. Accounts that want recall
over whole transcripts say ``full`` and pay for it.

Existing rows are backfilled to ``summaries_only`` rather than to ``full``:
nothing in the corpus is deleted by narrowing the scope, and an account that
was embedding everything keeps its vectors and simply stops adding new
transcript ones until it opts back in.

Revision ID: 20260916_embedding_scope
Revises: 20260915_session_backfill
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260916_embedding_scope"
down_revision: Union[str, None] = "20260915_session_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

DEFAULT_SCOPE = "summaries_only"


def upgrade() -> None:
    """Add ``scope`` and backfill every existing row to the default."""
    op.add_column(
        "session_embedding_setting",
        sa.Column(
            "scope",
            sa.String(length=32),
            nullable=False,
            server_default=DEFAULT_SCOPE,
        ),
    )
    # The server default already covers rows added by this statement; the
    # update is here so the intent survives a later default change.
    op.execute(
        "UPDATE session_embedding_setting "
        f"SET scope = '{DEFAULT_SCOPE}' WHERE scope IS NULL"
    )


def downgrade() -> None:
    """Drop ``scope``; the worker then embeds every chunk again."""
    op.drop_column("session_embedding_setting", "scope")
