"""Encrypted runtime-session artifacts, isolated per account.

Screenshots and recordings hang off a runtime session. Bytes are stored as
ciphertext; a partial unique index makes a repeated source reference
idempotent without collapsing rows that have no source-native id.

Revision ID: 20260924_session_artifact
Revises: 20260921_auth_generation

The revision id is shorter than the filename: ``alembic_version.version_num``
is ``varchar(32)``, and ``20260924_runtime_session_artifact`` does not fit.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260924_session_artifact"
down_revision: Union[str, None] = "20260921_auth_generation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_TABLE = "runtime_session_artifact"


def upgrade() -> None:
    """Create the artifact table, its indexes, and the partial unique key."""
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "runtime_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "activity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runtime_session_activity.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "manifest",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column(
            "availability",
            sa.String(length=20),
            nullable=False,
            server_default="available",
        ),
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_runtime_session_artifact_id"), _TABLE, ["id"])
    op.create_index(
        op.f("ix_runtime_session_artifact_account_id"), _TABLE, ["account_id"]
    )
    op.create_index(
        op.f("ix_runtime_session_artifact_runtime_session_id"),
        _TABLE,
        ["runtime_session_id"],
    )
    op.create_index(
        op.f("ix_runtime_session_artifact_activity_id"), _TABLE, ["activity_id"]
    )
    op.create_index(
        op.f("ix_runtime_session_artifact_legal_hold"), _TABLE, ["legal_hold"]
    )
    op.create_index(
        op.f("ix_runtime_session_artifact_expires_at"), _TABLE, ["expires_at"]
    )
    op.create_index(
        "uq_runtime_session_artifact_source",
        _TABLE,
        ["runtime_session_id", "kind", "source", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop runtime-session artifacts. Session rows are left in place."""
    op.drop_index("uq_runtime_session_artifact_source", table_name=_TABLE)
    op.drop_index(op.f("ix_runtime_session_artifact_expires_at"), table_name=_TABLE)
    op.drop_index(op.f("ix_runtime_session_artifact_legal_hold"), table_name=_TABLE)
    op.drop_index(op.f("ix_runtime_session_artifact_activity_id"), table_name=_TABLE)
    op.drop_index(
        op.f("ix_runtime_session_artifact_runtime_session_id"), table_name=_TABLE
    )
    op.drop_index(op.f("ix_runtime_session_artifact_account_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_runtime_session_artifact_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
