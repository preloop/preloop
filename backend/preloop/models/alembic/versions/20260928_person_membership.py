"""Link every user row to a person and mark it a direct membership.

Revision ID: 20260928_person_membership
Revises: 20260928_access_grants
Create Date: 2026-09-28

Third of four revisions for the account hierarchy (#986). One membership is
one ``user`` row; a ``person`` links the rows of one human.

Backfill:

* every row becomes ``membership_kind = 'direct'``;
* rows whose normalized emails (``lower(btrim(email))``) are equal and
  verified share one person, whose primary row is the one with the most
  recent login. Only one row per account can join a person (UNIQUE
  ``(person_id, account_id)``); a second verified row with the same address
  in the same account keeps a provisional person of its own;
* every other row, including every unverified duplicate, gets a provisional
  person of its own (``email_verified_at`` NULL). A provisional person is
  never merged here, so an address pre-registered without verification
  cannot capture somebody else's memberships.

The revision only links rows. It changes no credential and sends nothing:
passwords, passkeys and OAuth links stay on every row. Idempotent: rows that
already have a person are left alone and every DDL step checks for what it
creates.
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

_USER_CONSTRAINTS = (
    "ck_user_inherited_has_grant",
    "ck_user_membership_kind",
    "uq_user_person_account",
    "fk_user_access_grant",
    "fk_user_person",
)

# Most recent login first; rows that never logged in last; then newest row.
_RECENCY = "last_login DESC NULLS LAST, created_at DESC, id"

_BACKFILL_VERIFIED = f"""
WITH verified AS (
    SELECT
        u.id,
        lower(btrim(u.email)) AS email_normalized,
        u.last_login,
        u.created_at,
        row_number() OVER (
            PARTITION BY lower(btrim(u.email)), u.account_id ORDER BY {_RECENCY}
        ) AS account_rank
    FROM "user" u
    WHERE u.person_id IS NULL
      AND u.email_verified IS TRUE
      AND NOT EXISTS (
          SELECT 1 FROM person p
          WHERE p.email_normalized = lower(btrim(u.email))
            AND p.email_verified_at IS NOT NULL
      )
),
eligible AS (
    SELECT
        id,
        email_normalized,
        row_number() OVER (
            PARTITION BY email_normalized ORDER BY {_RECENCY}
        ) AS person_rank
    FROM verified
    WHERE account_rank = 1
),
persons AS (
    SELECT email_normalized, gen_random_uuid() AS person_id
    FROM eligible
    WHERE person_rank = 1
)
INSERT INTO _person_backfill (user_id, person_id, email_normalized, verified, is_primary)
SELECT e.id, p.person_id, e.email_normalized, TRUE, e.person_rank = 1
FROM eligible e
JOIN persons p USING (email_normalized)
"""

_BACKFILL_PROVISIONAL = """
INSERT INTO _person_backfill (user_id, person_id, email_normalized, verified, is_primary)
SELECT u.id, gen_random_uuid(), lower(btrim(u.email)), FALSE, TRUE
FROM "user" u
WHERE u.person_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM _person_backfill b WHERE b.user_id = u.id)
"""

_INSERT_PERSONS = """
INSERT INTO person (
    id, created_at, updated_at, email_normalized, email_verified_at,
    primary_user_id, last_active_user_id
)
SELECT
    person_id, now(), now(), email_normalized,
    CASE WHEN verified THEN now() END,
    user_id, user_id
FROM _person_backfill
WHERE is_primary
"""

_LINK_USERS = """
UPDATE "user" u
SET person_id = b.person_id
FROM _person_backfill b
WHERE u.id = b.user_id
"""


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _constraint_exists(name: str, table: str) -> bool:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_constraint"
                " WHERE conname = :name AND conrelid = to_regclass(:table)"
            ),
            {"name": name, "table": f'public."{table}"'},
        )
        .first()
        is not None
    )


def upgrade() -> None:
    """Create person, add the membership columns, backfill, then constrain."""
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
                    "lower(btrim(email)) of the membership rows this person holds"
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

    op.execute(
        "CREATE TEMP TABLE _person_backfill ("
        " user_id UUID PRIMARY KEY,"
        " person_id UUID NOT NULL,"
        " email_normalized VARCHAR(255) NOT NULL,"
        " verified BOOLEAN NOT NULL,"
        " is_primary BOOLEAN NOT NULL"
        ") ON COMMIT DROP"
    )
    op.execute(_BACKFILL_VERIFIED)
    op.execute(_BACKFILL_PROVISIONAL)
    op.execute(_INSERT_PERSONS)
    op.execute(_LINK_USERS)
    op.execute("DROP TABLE _person_backfill")

    op.execute('ALTER TABLE "user" ALTER COLUMN person_id SET NOT NULL')
    if not _constraint_exists("fk_user_person", "user"):
        op.create_foreign_key(
            "fk_user_person",
            "user",
            "person",
            ["person_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _constraint_exists("fk_user_access_grant", "user"):
        op.create_foreign_key(
            "fk_user_access_grant",
            "user",
            "account_access_grant",
            ["access_grant_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _constraint_exists("uq_user_person_account", "user"):
        op.create_unique_constraint(
            "uq_user_person_account", "user", ["person_id", "account_id"]
        )
    if not _constraint_exists("ck_user_membership_kind", "user"):
        op.create_check_constraint(
            "ck_user_membership_kind",
            "user",
            "membership_kind IN ('direct', 'inherited')",
        )
    if not _constraint_exists("ck_user_inherited_has_grant", "user"):
        op.create_check_constraint(
            "ck_user_inherited_has_grant",
            "user",
            "(membership_kind = 'inherited') = (access_grant_id IS NOT NULL)",
        )
    op.execute(
        'CREATE INDEX IF NOT EXISTS ix_user_access_grant_id ON "user" (access_grant_id)'
    )


def downgrade() -> None:
    """Drop the membership columns and the person table."""
    op.execute("DROP INDEX IF EXISTS ix_user_access_grant_id")
    for name in _USER_CONSTRAINTS:
        op.execute(f'ALTER TABLE "user" DROP CONSTRAINT IF EXISTS {name}')
    op.execute(
        'ALTER TABLE "user"'
        " DROP COLUMN IF EXISTS access_grant_id,"
        " DROP COLUMN IF EXISTS membership_kind,"
        " DROP COLUMN IF EXISTS person_id"
    )
    op.execute("DROP TABLE IF EXISTS person")
