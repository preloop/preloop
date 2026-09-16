"""Store named session searches so a repeated question can be re-run.

One table. A row is a question (query text, mode, validated filters, snippet
preferences), never an answer: nothing here caches results, so a re-run sees
the corpus as it is at re-run time, including whatever has since been
redacted, retained out or deleted.

``ranking_identity`` and ``filters_version`` are the honesty columns. The
first records which ranking constants were in force when the search was saved,
so a run whose constants have moved can say the ordering is no longer the one
the search was saved under instead of pretending the answer is comparable. The
second records which filter schema the payload was validated against.

Visibility defaults to ``private``; sharing with the account is an explicit
update (issue #673 open decision 1).

Revision ID: 20260916_session_saved_search
Revises: 20260915_session_backfill
Create Date: 2026-09-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260916_session_saved_search"
down_revision: Union[str, None] = "20260915_session_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the saved search table and the indexes its reads need."""
    op.create_table(
        "session_saved_search",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column(
            "filters",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "filters_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "max_snippets_per_session",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
        sa.Column(
            "include_snippet_text",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="private",
        ),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ranking_identity", sa.String(length=128), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "account_id",
            "owner_user_id",
            "name",
            name="uq_session_saved_search_owner_name",
        ),
    )
    op.create_index(
        op.f("ix_session_saved_search_id"),
        "session_saved_search",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_saved_search_account_id"),
        "session_saved_search",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_saved_search_owner_user_id"),
        "session_saved_search",
        ["owner_user_id"],
        unique=False,
    )
    # The list read is "mine, plus the ones shared with my account", which is
    # these two indexes and nothing else.
    op.create_index(
        "ix_session_saved_search_account_visibility",
        "session_saved_search",
        ["account_id", "visibility"],
        unique=False,
    )
    op.create_index(
        "ix_session_saved_search_account_owner",
        "session_saved_search",
        ["account_id", "owner_user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the saved search table. Saved questions are lost, not results."""
    op.drop_index(
        "ix_session_saved_search_account_owner", table_name="session_saved_search"
    )
    op.drop_index(
        "ix_session_saved_search_account_visibility", table_name="session_saved_search"
    )
    op.drop_index(
        op.f("ix_session_saved_search_owner_user_id"),
        table_name="session_saved_search",
    )
    op.drop_index(
        op.f("ix_session_saved_search_account_id"), table_name="session_saved_search"
    )
    op.drop_index(op.f("ix_session_saved_search_id"), table_name="session_saved_search")
    op.drop_table("session_saved_search")
