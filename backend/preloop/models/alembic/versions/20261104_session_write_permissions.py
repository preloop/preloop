"""Seed session sharing and optimization permissions for existing roles.

Revision ID: 20261104_session_write_perms
Revises: 20261103_gov_parse_retry
"""

from alembic import op
import sqlalchemy as sa

revision = "20261104_session_write_perms"
down_revision = "20261103_gov_parse_retry"
branch_labels = None
depends_on = None

PERMISSIONS = (
    ("share_session_searches", "Share saved session searches with the account"),
    (
        "run_session_optimizations",
        "Run and store runtime session optimization analysis",
    ),
)


def upgrade() -> None:
    """Add catalogue entries and missing grants without changing custom roles."""
    for name, description in PERMISSIONS:
        op.execute(
            sa.text(
                """
                INSERT INTO permission
                    (id, name, description, category, is_active, created_at, updated_at)
                VALUES
                    (gen_random_uuid(), :name, :description, 'sessions', true, now(), now())
                ON CONFLICT (name) DO NOTHING
                """
            ).bindparams(name=name, description=description)
        )
        op.execute(
            sa.text(
                """
                INSERT INTO role_permission
                    (id, role_id, permission_id, granted_at, created_at, updated_at)
                SELECT gen_random_uuid(), r.id, p.id, now(), now(), now()
                FROM role AS r
                JOIN permission AS p ON p.name = :name
                WHERE r.is_system_role = true AND r.account_id IS NULL
                    AND r.name IN ('owner', 'admin', 'editor', 'executor')
                ON CONFLICT (role_id, permission_id) DO NOTHING
                """
            ).bindparams(name=name)
        )


def downgrade() -> None:
    """Remove the two catalogue entries and their dependent grants."""
    for name, _ in PERMISSIONS:
        op.execute(
            sa.text("DELETE FROM permission WHERE name = :name").bindparams(name=name)
        )
