"""Place every account in a tree: parent, root, materialized path, depth.

Revision ID: 20260928_account_hierarchy
Revises: 20260927_policy_notice_hit
Create Date: 2026-09-28

First of six revisions for the account hierarchy (#986). Every existing
account becomes a root: ``root_account_id = id``, ``hierarchy_path = [id]``,
depth 0. ``ck_account_hierarchy_depth_max`` is the only place the depth is
limited (one level below the root at launch).

Only ``account`` is touched here. The person backfill on ``user`` runs in its
own revision, so no transaction holds ACCESS EXCLUSIVE on both tables.
Idempotent: every step checks for what it creates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260928_account_hierarchy"
down_revision = "20260927_policy_notice_hit"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_CHECKS = {
    "ck_account_parent_iff_nonroot": (
        "(parent_account_id IS NULL) = (hierarchy_depth = 0)"
    ),
    "ck_account_hierarchy_depth_max": "hierarchy_depth <= 1",
    "ck_account_hierarchy_path_shape": (
        "hierarchy_depth >= 0"
        " AND cardinality(hierarchy_path) = hierarchy_depth + 1"
        " AND hierarchy_path[1] = root_account_id"
        " AND hierarchy_path[cardinality(hierarchy_path)] = id"
    ),
    # parent_account_id and hierarchy_path encode the same edge: keep them
    # equal so the FK that guards deletes and the tree helpers agree.
    "ck_account_parent_is_path_tail": (
        "hierarchy_depth = 0 OR parent_account_id = hierarchy_path[hierarchy_depth]"
    ),
}

_COMMENTS = {
    "parent_account_id": "Parent account; NULL for a root account",
    "root_account_id": "Root of this account''s tree; equals id for a root account",
    "hierarchy_path": "Account ids from the root to this account, both included",
    "hierarchy_depth": "Levels below the root; 0 for a root account",
}


def _constraint_exists(name: str, table: str = "account") -> bool:
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
    """Add the hierarchy columns, make every account a root, then constrain."""
    op.execute(
        "ALTER TABLE account"
        " ADD COLUMN IF NOT EXISTS parent_account_id UUID,"
        " ADD COLUMN IF NOT EXISTS root_account_id UUID,"
        " ADD COLUMN IF NOT EXISTS hierarchy_path UUID[],"
        " ADD COLUMN IF NOT EXISTS hierarchy_depth SMALLINT NOT NULL DEFAULT 0"
    )
    for column, comment in _COMMENTS.items():
        op.execute(f"COMMENT ON COLUMN account.{column} IS '{comment}'")
    op.execute(
        "UPDATE account"
        " SET root_account_id = id, hierarchy_path = ARRAY[id], hierarchy_depth = 0"
        " WHERE root_account_id IS NULL OR hierarchy_path IS NULL"
    )
    op.execute(
        "ALTER TABLE account"
        " ALTER COLUMN root_account_id SET NOT NULL,"
        " ALTER COLUMN hierarchy_path SET NOT NULL"
    )
    if not _constraint_exists("fk_account_parent"):
        op.create_foreign_key(
            "fk_account_parent",
            "account",
            "account",
            ["parent_account_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _constraint_exists("fk_account_root"):
        op.create_foreign_key(
            "fk_account_root", "account", "account", ["root_account_id"], ["id"]
        )
    for name, condition in _CHECKS.items():
        if not _constraint_exists(name):
            op.create_check_constraint(name, "account", condition)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_account_parent_account_id"
        " ON account (parent_account_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_account_root_account_id"
        " ON account (root_account_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_account_hierarchy_path"
        " ON account USING gin (hierarchy_path)"
    )


def downgrade() -> None:
    """Drop the hierarchy columns (and with them their indexes and checks)."""
    for name in (*_CHECKS, "fk_account_root", "fk_account_parent"):
        op.execute(f"ALTER TABLE account DROP CONSTRAINT IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS ix_account_hierarchy_path")
    op.execute("DROP INDEX IF EXISTS ix_account_root_account_id")
    op.execute("DROP INDEX IF EXISTS ix_account_parent_account_id")
    op.execute(
        "ALTER TABLE account"
        " DROP COLUMN IF EXISTS hierarchy_depth,"
        " DROP COLUMN IF EXISTS hierarchy_path,"
        " DROP COLUMN IF EXISTS root_account_id,"
        " DROP COLUMN IF EXISTS parent_account_id"
    )
