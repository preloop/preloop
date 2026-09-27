"""Create person and add the membership columns to user, nullable.

Revision ID: 20260928_person_membership
Revises: 20260928_access_grants
Create Date: 2026-09-28

Third of six revisions for the account hierarchy (#986). One membership is
one ``user`` row; a ``person`` links the rows of one human.

The person work on ``"user"`` is split in three so no single transaction holds
``ACCESS EXCLUSIVE`` on ``"user"`` across the backfill:

1. this revision: create ``person`` and add ``person_id``, ``membership_kind``
   and ``access_grant_id`` to ``"user"``, all catalog-only (``person_id`` is
   nullable, the ``membership_kind`` default is a constant). The lock is held
   for milliseconds;
2. ``20260928_person_backfill``: link every row to a person. Row locks only;
   the API keeps reading and writing ``"user"``;
3. ``20260928_person_constraints``: link rows written since, then ``SET NOT
   NULL``, the foreign keys, ``uq_user_person_account`` and the checks. This
   one holds ``ACCESS EXCLUSIVE`` on ``"user"`` for a full-table scan and one
   unique index build; see its docstring for when to drain.

Idempotent: every DDL step checks for what it creates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260928_person_membership"
down_revision = "20260928_access_grants"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    """Create person and add the nullable membership columns."""
    if not _has_table("person"):
        op.create_table(
            "person",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
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
            sa.Column(
                "email_normalized",
                sa.String(255),
                nullable=False,
                comment=(
                    "Lowercased, trimmed address of the membership rows this person holds"
                ),
            ),
            sa.Column(
                "email_verified_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment=(
                    "When the address was known verified; NULL for a provisional person"
                ),
            ),
            sa.Column(
                "primary_user_id",
                UUID(as_uuid=True),
                sa.ForeignKey(
                    "user.id", ondelete="SET NULL", name="fk_person_primary_user"
                ),
                nullable=True,
                comment="The membership row holding password, passkeys and OAuth links",
            ),
            sa.Column(
                "last_active_user_id",
                UUID(as_uuid=True),
                sa.ForeignKey(
                    "user.id", ondelete="SET NULL", name="fk_person_last_active_user"
                ),
                nullable=True,
                comment="The membership row this person used most recently",
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_person_id ON person (id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_person_email_normalized"
        " ON person (email_normalized)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_person_email_verified"
        " ON person (email_normalized) WHERE email_verified_at IS NOT NULL"
    )

    op.execute(
        'ALTER TABLE "user"'
        " ADD COLUMN IF NOT EXISTS person_id UUID,"
        " ADD COLUMN IF NOT EXISTS membership_kind VARCHAR(16)"
        " NOT NULL DEFAULT 'direct',"
        " ADD COLUMN IF NOT EXISTS access_grant_id UUID"
    )
    op.execute(
        'COMMENT ON COLUMN "user".person_id IS'
        " 'The person this membership row belongs to'"
    )
    op.execute(
        'COMMENT ON COLUMN "user".membership_kind IS'
        " 'direct | inherited (created by an account access grant)'"
    )
    op.execute(
        'COMMENT ON COLUMN "user".access_grant_id IS'
        " 'Grant that created an inherited membership'"
    )


def downgrade() -> None:
    """Drop the membership columns and the person table."""
    op.execute(
        'ALTER TABLE "user"'
        " DROP COLUMN IF EXISTS access_grant_id,"
        " DROP COLUMN IF EXISTS membership_kind,"
        " DROP COLUMN IF EXISTS person_id"
    )
    op.execute("DROP TABLE IF EXISTS person")
