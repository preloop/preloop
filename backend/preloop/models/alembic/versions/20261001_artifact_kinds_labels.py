"""Add name, labels and provenance columns to runtime-session artifacts.

Revision ID: 20261001_artifact_kinds_labels
Revises: 20260927_issue_cost_accuracy
Create Date: 2026-10-01

Additive only, every new column is nullable. ``labels`` is account-defined
JSON metadata (GIN index for containment filters), ``producer`` records the
ingest path, ``agent_id`` and ``tool_name`` the agent and tool that produced
the bytes, ``text_status`` whether text was extracted, and
``parent_artifact_id`` lineage (for example a summary derived from a
transcript). Rows that exist before this revision were all written by the
browser-step path, so they are backfilled with ``producer='browser_steps'``.
Indexes are created inside the migration transaction, not CONCURRENTLY: the
table is new (20260924) and still small.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20261001_artifact_kinds_labels"
down_revision = "20260927_issue_cost_accuracy"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_TABLE = "runtime_session_artifact"
_PARENT_FK = "fk_runtime_session_artifact_parent"


def upgrade() -> None:
    """Add the columns, indexes and the browser_steps producer backfill."""
    op.add_column(_TABLE, sa.Column("name", sa.String(255), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "labels",
            JSONB(),
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(_TABLE, sa.Column("producer", sa.String(32), nullable=True))
    op.add_column(_TABLE, sa.Column("agent_id", UUID(as_uuid=True), nullable=True))
    op.add_column(_TABLE, sa.Column("tool_name", sa.String(255), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "text_status",
            sa.String(16),
            nullable=True,
            server_default=sa.text("'none'"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("parent_artifact_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        _PARENT_FK,
        _TABLE,
        _TABLE,
        ["parent_artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_runtime_session_artifact_parent_artifact_id",
        _TABLE,
        ["parent_artifact_id"],
    )
    op.create_index(
        "ix_runtime_session_artifact_labels",
        _TABLE,
        ["labels"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_runtime_session_artifact_account_kind_created",
        _TABLE,
        ["account_id", "kind", "created_at"],
    )
    op.execute(
        sa.text(
            "UPDATE runtime_session_artifact SET producer = 'browser_steps' "
            "WHERE producer IS NULL"
        )
    )


def downgrade() -> None:
    """Drop the indexes and columns added by :func:`upgrade`."""
    op.drop_index("ix_runtime_session_artifact_account_kind_created", _TABLE)
    op.drop_index("ix_runtime_session_artifact_labels", _TABLE)
    op.drop_index("ix_runtime_session_artifact_parent_artifact_id", _TABLE)
    op.drop_constraint(_PARENT_FK, _TABLE, type_="foreignkey")
    for column in (
        "parent_artifact_id",
        "text_status",
        "tool_name",
        "agent_id",
        "producer",
        "labels",
        "name",
    ):
        op.drop_column(_TABLE, column)
