"""Seed the basic OSS member role without changing existing role assignments."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "20261029_basic_member_role"
down_revision = "20261110_media_timing"
branch_labels = None
depends_on = None

_MEMBER_PERMISSIONS = (
    "view_flows",
    "view_tools",
    "view_policies",
    "view_mcp_servers",
    "view_trackers",
    "view_projects",
    "view_organizations",
    "view_issues",
    "view_compliance",
    "view_ai_models",
    "view_agents",
    "view_runtime_sessions",
    "view_teams",
    "view_cost",
    "view_approvals",
    "decide_approvals",
    "execute_tools",
    "execute_flows",
    "manage_agents",
    "control_managed_agent",
    "manage_runtime_sessions",
)


def upgrade() -> None:
    """Add a safe member role for upgraded installations."""
    connection = op.get_bind()
    role_id = connection.execute(
        sa.text(
            "SELECT id FROM role WHERE name = 'member' AND account_id IS NULL "
            "AND is_system_role = true"
        )
    ).scalar()
    if role_id is None:
        role_id = uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO role (id, name, description, is_system_role) "
                "VALUES (:id, 'member', :description, true)"
            ),
            {
                "id": role_id,
                "description": "Account member with basic operation and usage access",
            },
        )
    permissions = connection.execute(
        sa.text("SELECT id FROM permission WHERE name IN :names").bindparams(
            sa.bindparam("names", expanding=True)
        ),
        {"names": _MEMBER_PERMISSIONS},
    ).scalars()
    for permission_id in permissions:
        connection.execute(
            sa.text(
                "INSERT INTO role_permission (id, role_id, permission_id) "
                "SELECT :id, :role_id, :permission_id WHERE NOT EXISTS "
                "(SELECT 1 FROM role_permission WHERE role_id = :role_id "
                "AND permission_id = :permission_id)"
            ),
            {"id": uuid4(), "role_id": role_id, "permission_id": permission_id},
        )


def downgrade() -> None:
    """Keep the additive role so existing member assignments remain valid."""
    pass
