"""Constrain the membership columns on user once every row has a person.

Revision ID: 20260928_person_constraints
Revises: 20260928_person_backfill
Create Date: 2026-09-28

Fifth of six revisions for the account hierarchy (#986).

Locking: this revision takes ``ACCESS EXCLUSIVE`` on ``"user"`` first and
holds it to commit, across the full-table scan for ``SET NOT NULL``, the two
foreign key and two check validations, and the ``uq_user_person_account`` and
``ix_user_access_grant_id`` index builds. The row-by-row backfill ran in
``20260928_person_backfill`` without that lock. Every query on ``"user"``
(every login and API request) waits while this runs: 2 to 3 s on one million
``"user"`` rows on a local Postgres 16 (3.0 s cold, 1.8 to 1.9 s warm). If a
stall of that length is not acceptable, or the table is far larger (a
``statement_timeout`` kill is not retried), drain the API first
(``docs/operations/schema-migrations.md``, "When to drain the API first").

Rows inserted after the backfill committed, by pods still running the
previous release, have no person yet. They are linked first, under the lock,
each to a provisional person of its own (``email_verified_at`` NULL), which is
the same thing the ORM hook does for a new row whose address is taken.

Idempotent: every step checks for what it creates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260928_person_constraints"
down_revision = "20260928_person_backfill"
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

_LINK_STRAGGLERS = """
WITH stragglers AS (
    SELECT id, lower(btrim(email, E' \\t\\n\\r\\f\\x0b')) AS email_normalized,
           gen_random_uuid() AS person_id
    FROM "user"
    WHERE person_id IS NULL
),
persons AS (
    INSERT INTO person (
        id, created_at, updated_at, email_normalized, email_verified_at,
        primary_user_id, last_active_user_id
    )
    SELECT person_id, now(), now(), email_normalized, NULL, id, id
    FROM stragglers
)
UPDATE "user" u
SET person_id = s.person_id
FROM stragglers s
WHERE u.id = s.id
"""


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
    """Link late rows, then SET NOT NULL, foreign keys, unique and checks."""
    op.execute('LOCK TABLE "user" IN ACCESS EXCLUSIVE MODE')
    op.execute(_LINK_STRAGGLERS)

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
    """Drop the constraints and make person_id nullable again."""
    op.execute("DROP INDEX IF EXISTS ix_user_access_grant_id")
    for name in _USER_CONSTRAINTS:
        op.execute(f'ALTER TABLE "user" DROP CONSTRAINT IF EXISTS {name}')
    op.execute(
        "DO $$ BEGIN"
        " IF EXISTS (SELECT 1 FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = 'user'"
        " AND column_name = 'person_id') THEN"
        ' ALTER TABLE "user" ALTER COLUMN person_id DROP NOT NULL;'
        " END IF; END $$"
    )
