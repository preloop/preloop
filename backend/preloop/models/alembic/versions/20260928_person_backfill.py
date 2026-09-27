"""Link every user row to a person and mark it a direct membership.

Revision ID: 20260928_person_backfill
Revises: 20260928_person_membership
Create Date: 2026-09-28

Fourth of six revisions for the account hierarchy (#986). Runs with row locks
only: ``person_id`` is still nullable here, so the API keeps serving while
the backfill updates ``"user"``. Rows written after this commits are linked
by ``20260928_person_constraints``.

* every row is ``membership_kind = 'direct'`` (the column default);
* rows whose normalized emails are equal and verified share one person, whose
  primary row is the one with the most recent login. Only one row per account
  can join a person (UNIQUE ``(person_id, account_id)``); a second verified
  row with the same address in the same account keeps a provisional person
  of its own;
* every other row, including every unverified duplicate, gets a provisional
  person of its own (``email_verified_at`` NULL). A provisional person is
  never merged here, so an address pre-registered without verification
  cannot capture somebody else's memberships.

The revision only links rows. It changes no credential and sends nothing:
passwords, passkeys and OAuth links stay on every row. Idempotent: rows that
already have a person are left alone.
"""

from __future__ import annotations

from alembic import op

revision = "20260928_person_backfill"
down_revision = "20260928_person_membership"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def normalized_email(column: str) -> str:
    """SQL for the normalized form of ``column``: trimmed, lowercased."""
    return f"lower(btrim({column}))"


# Most recent login first; rows that never logged in last; then newest row.
_RECENCY = "last_login DESC NULLS LAST, created_at DESC, id"

_BACKFILL_VERIFIED = f"""
WITH verified AS (
    SELECT
        u.id,
        {normalized_email("u.email")} AS email_normalized,
        u.last_login,
        u.created_at,
        row_number() OVER (
            PARTITION BY {normalized_email("u.email")}, u.account_id
            ORDER BY {_RECENCY}
        ) AS account_rank
    FROM "user" u
    WHERE u.person_id IS NULL
      AND u.email_verified IS TRUE
      AND NOT EXISTS (
          SELECT 1 FROM person p
          WHERE p.email_normalized = {normalized_email("u.email")}
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

_BACKFILL_PROVISIONAL = f"""
INSERT INTO _person_backfill (user_id, person_id, email_normalized, verified, is_primary)
SELECT u.id, gen_random_uuid(), {normalized_email("u.email")}, FALSE, TRUE
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


def upgrade() -> None:
    """Give every unlinked user row a person."""
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


def downgrade() -> None:
    """Nothing to undo: 20260928_person_membership drops the column and table."""
