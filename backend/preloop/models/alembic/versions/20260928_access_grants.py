"""Add account access grants and tag grant-created roles.

Revision ID: 20260928_access_grants
Revises: 20260928_account_hierarchy
Create Date: 2026-09-28

Second of six revisions for the account hierarchy (#986).
``account_access_grant`` records access a parent account gives its user or
team in all or selected subaccounts; ``account_access_grant_target`` lists the
selected ones. ``user_role.access_grant_id`` marks the roles a grant created,
so revoking the grant removes exactly those. No rows are written.
Idempotent: every step checks for what it creates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260928_access_grants"
down_revision = "20260928_account_hierarchy"
branch_labels = None
depends_on = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


_TEAM_DELETED_FUNCTION = """
CREATE OR REPLACE FUNCTION preloop_team_deleted() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM account_access_grant
    WHERE subject_type = 'team' AND subject_id = OLD.id;
    RETURN NULL;
END
$$
"""


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    return any(
        existing["name"] == column
        for existing in sa.inspect(op.get_bind()).get_columns(table)
    )


def upgrade() -> None:
    """Create the grant tables and add user_role.access_grant_id."""
    if not _has_table("account_access_grant"):
        op.create_table(
            "account_access_grant",
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
                "parent_account_id",
                UUID(as_uuid=True),
                sa.ForeignKey("account.id", ondelete="CASCADE"),
                nullable=False,
                comment="The parent account issuing the grant",
            ),
            sa.Column(
                "subject_type", sa.String(16), nullable=False, comment="user | team"
            ),
            sa.Column(
                "subject_id",
                UUID(as_uuid=True),
                nullable=False,
                comment="user.id or team.id in the parent account",
            ),
            sa.Column(
                "access_level",
                sa.String(16),
                nullable=False,
                comment="read | operate | admin",
            ),
            sa.Column(
                "target_mode",
                sa.String(16),
                nullable=False,
                comment=(
                    "all subaccounts, or the selected ones in "
                    "account_access_grant_target"
                ),
            ),
            sa.Column(
                "created_by",
                UUID(as_uuid=True),
                sa.ForeignKey("user.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "revoked_by",
                UUID(as_uuid=True),
                sa.ForeignKey("user.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "subject_type IN ('user', 'team')",
                name="ck_account_access_grant_subject_type",
            ),
            sa.CheckConstraint(
                "access_level IN ('read', 'operate', 'admin')",
                name="ck_account_access_grant_access_level",
            ),
            sa.CheckConstraint(
                "target_mode IN ('all', 'selected')",
                name="ck_account_access_grant_target_mode",
            ),
        )
        op.create_index("ix_account_access_grant_id", "account_access_grant", ["id"])
        op.create_index(
            "ix_account_access_grant_parent_subject",
            "account_access_grant",
            ["parent_account_id", "subject_type", "subject_id"],
        )

    if not _has_table("account_access_grant_target"):
        op.create_table(
            "account_access_grant_target",
            sa.Column(
                "grant_id",
                UUID(as_uuid=True),
                sa.ForeignKey("account_access_grant.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "subaccount_id",
                UUID(as_uuid=True),
                sa.ForeignKey("account.id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )
        op.create_index(
            "ix_account_access_grant_target_subaccount_id",
            "account_access_grant_target",
            ["subaccount_id"],
        )

    if not _has_column("user_role", "access_grant_id"):
        op.add_column(
            "user_role",
            sa.Column(
                "access_grant_id",
                UUID(as_uuid=True),
                sa.ForeignKey(
                    "account_access_grant.id",
                    ondelete="CASCADE",
                    name="fk_user_role_access_grant",
                ),
                nullable=True,
                comment=(
                    "Grant that created this role; NULL for a role assigned directly"
                ),
            ),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_role_access_grant_id"
        " ON user_role (access_grant_id)"
    )
    # subject_id is polymorphic (user or team), so no foreign key can cascade.
    # Deleting a team deletes its grants here; 20260928_person_constraints does
    # the same for users.
    op.execute(_TEAM_DELETED_FUNCTION)
    op.execute("DROP TRIGGER IF EXISTS trg_team_deleted ON team")
    op.execute(
        "CREATE TRIGGER trg_team_deleted AFTER DELETE ON team"
        " FOR EACH ROW EXECUTE FUNCTION preloop_team_deleted()"
    )


def downgrade() -> None:
    """Drop the team trigger, user_role.access_grant_id and the grant tables."""
    op.execute("DROP TRIGGER IF EXISTS trg_team_deleted ON team")
    op.execute("DROP FUNCTION IF EXISTS preloop_team_deleted()")
    op.execute("DROP INDEX IF EXISTS ix_user_role_access_grant_id")
    op.execute("ALTER TABLE user_role DROP COLUMN IF EXISTS access_grant_id")
    op.execute("DROP TABLE IF EXISTS account_access_grant_target")
    op.execute("DROP TABLE IF EXISTS account_access_grant")
