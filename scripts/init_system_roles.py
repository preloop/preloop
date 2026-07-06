"""Initialize system roles and permissions.

This script creates the default system roles and permissions for the RBAC system.
It should be run during application startup or database initialization.

System Roles:
- owner: Full access including billing, user management, account closure
- admin: Full access except billing and account closure
- editor: Create/edit flows, tools, trackers, projects
- executor: Execute flows, trigger tools
- tracker_manager: Add/edit trackers, sync data
- analyst: Read-only + compliance/dependency/duplicate detection
- viewer: Read-only access
"""

import logging
import dotenv

from typing import Dict, List

from sqlalchemy.orm import Session

from preloop.models.models.permission import Permission, Role, RolePermission

logger = logging.getLogger(__name__)
dotenv.load_dotenv()

# Define all system permissions by category
SYSTEM_PERMISSIONS: Dict[str, List[Dict[str, str]]] = {
    "account": [
        {
            "name": "manage_account",
            "description": "Manage account settings and configuration",
        },
        {"name": "close_account", "description": "Close/delete the account"},
    ],
    "billing": [
        {
            "name": "view_billing",
            "description": "View billing information and invoices",
        },
        {
            "name": "manage_billing",
            "description": "Manage payment methods and billing settings",
        },
    ],
    "users": [
        {"name": "view_users", "description": "View users in the account"},
        {"name": "invite_users", "description": "Invite new users to the account"},
        {"name": "manage_users", "description": "Add, edit, and remove users"},
        {
            "name": "assign_roles",
            "description": "Assign roles and permissions to users",
        },
    ],
    "teams": [
        {"name": "view_teams", "description": "View teams in the account"},
        {
            "name": "manage_teams",
            "description": "Create, edit, and delete teams and manage team memberships",
        },
    ],
    "flows": [
        {"name": "view_flows", "description": "View flows in the account"},
        {"name": "create_flows", "description": "Create new flows"},
        {"name": "edit_flows", "description": "Edit existing flows"},
        {"name": "delete_flows", "description": "Delete flows"},
        {"name": "execute_flows", "description": "Execute/run flows"},
    ],
    "tools": [
        {"name": "view_tools", "description": "View tools and tool configurations"},
        {"name": "manage_tools", "description": "Add, configure, and remove tools"},
        {"name": "execute_tools", "description": "Execute/use tools"},
    ],
    "mcp_servers": [
        {"name": "view_mcp_servers", "description": "View MCP servers"},
        {"name": "create_mcp_servers", "description": "Create new MCP servers"},
        {"name": "edit_mcp_servers", "description": "Edit existing MCP servers"},
        {"name": "delete_mcp_servers", "description": "Delete MCP servers"},
        {
            "name": "manage_mcp_servers",
            "description": "Full management of MCP servers including scanning",
        },
    ],
    "trackers": [
        {"name": "view_trackers", "description": "View tracker configurations"},
        {
            "name": "manage_trackers",
            "description": "Add, configure, and remove trackers",
        },
        {"name": "sync_trackers", "description": "Trigger tracker synchronization"},
    ],
    "projects": [
        {"name": "view_projects", "description": "View projects"},
        {"name": "manage_projects", "description": "Create, edit, and delete projects"},
    ],
    "issues": [
        {"name": "view_issues", "description": "View issues"},
        {"name": "create_issues", "description": "Create new issues"},
        {"name": "edit_issues", "description": "Edit existing issues"},
        {"name": "delete_issues", "description": "Delete issues"},
        {"name": "comment_issues", "description": "Add comments to issues"},
    ],
    "compliance": [
        {
            "name": "view_compliance",
            "description": "View compliance checks and results",
        },
        {
            "name": "run_compliance",
            "description": "Run compliance checks and improve suggestions",
        },
    ],
    "analysis": [
        {
            "name": "detect_duplicates",
            "description": "Run duplicate detection on issues",
        },
        {
            "name": "manage_dependencies",
            "description": "Create and manage issue dependencies",
        },
    ],
    "agents": [
        {
            "name": "control_managed_agent",
            "description": "Send commands and prompts to managed agents",
        },
    ],
}

# Define system roles with their permissions
SYSTEM_ROLES: Dict[str, Dict[str, any]] = {
    "owner": {
        "description": "Account owner with full access to all features including billing and account management",
        "permissions": [
            # All permissions
            "manage_account",
            "close_account",
            "view_billing",
            "manage_billing",
            "view_users",
            "invite_users",
            "manage_users",
            "assign_roles",
            "view_teams",
            "manage_teams",
            "view_flows",
            "create_flows",
            "edit_flows",
            "delete_flows",
            "execute_flows",
            "view_tools",
            "manage_tools",
            "execute_tools",
            "view_mcp_servers",
            "create_mcp_servers",
            "edit_mcp_servers",
            "delete_mcp_servers",
            "manage_mcp_servers",
            "view_trackers",
            "manage_trackers",
            "sync_trackers",
            "view_projects",
            "manage_projects",
            "view_issues",
            "create_issues",
            "edit_issues",
            "delete_issues",
            "comment_issues",
            "view_compliance",
            "run_compliance",
            "detect_duplicates",
            "manage_dependencies",
            "control_managed_agent",
        ],
    },
    "admin": {
        "description": "Administrator with full access except billing and account closure",
        "permissions": [
            "manage_account",
            "view_billing",
            "view_users",
            "invite_users",
            "manage_users",
            "assign_roles",
            "view_teams",
            "manage_teams",
            "view_flows",
            "create_flows",
            "edit_flows",
            "delete_flows",
            "execute_flows",
            "view_tools",
            "manage_tools",
            "execute_tools",
            "view_mcp_servers",
            "create_mcp_servers",
            "edit_mcp_servers",
            "delete_mcp_servers",
            "manage_mcp_servers",
            "view_trackers",
            "manage_trackers",
            "sync_trackers",
            "view_projects",
            "manage_projects",
            "view_issues",
            "create_issues",
            "edit_issues",
            "delete_issues",
            "comment_issues",
            "view_compliance",
            "run_compliance",
            "detect_duplicates",
            "manage_dependencies",
            "control_managed_agent",
        ],
    },
    "editor": {
        "description": "Can create and edit flows, tools, trackers, and projects",
        "permissions": [
            "view_users",
            "view_teams",
            "view_flows",
            "create_flows",
            "edit_flows",
            "execute_flows",
            "view_tools",
            "manage_tools",
            "execute_tools",
            "view_mcp_servers",
            "create_mcp_servers",
            "edit_mcp_servers",
            "delete_mcp_servers",
            "manage_mcp_servers",
            "view_trackers",
            "manage_trackers",
            "sync_trackers",
            "view_projects",
            "manage_projects",
            "view_issues",
            "create_issues",
            "edit_issues",
            "comment_issues",
            "view_compliance",
            "run_compliance",
            "detect_duplicates",
            "manage_dependencies",
            "control_managed_agent",
        ],
    },
    "executor": {
        "description": "Can execute flows and use tools, but not modify them",
        "permissions": [
            "view_flows",
            "execute_flows",
            "view_tools",
            "execute_tools",
            "view_trackers",
            "view_projects",
            "view_issues",
            "create_issues",
            "comment_issues",
            "control_managed_agent",
        ],
    },
    "tracker_manager": {
        "description": "Specialized role for managing tracker integrations and syncing data",
        "permissions": [
            "view_trackers",
            "manage_trackers",
            "sync_trackers",
            "view_projects",
            "manage_projects",
            "view_issues",
            "create_issues",
            "edit_issues",
            "comment_issues",
        ],
    },
    "analyst": {
        "description": "Read-only access plus ability to run compliance checks and duplicate detection",
        "permissions": [
            "view_users",
            "view_teams",
            "view_flows",
            "view_tools",
            "view_trackers",
            "view_projects",
            "view_issues",
            "view_compliance",
            "run_compliance",
            "detect_duplicates",
            "manage_dependencies",
        ],
    },
    "viewer": {
        "description": "Read-only access to all resources",
        "permissions": [
            "view_users",
            "view_teams",
            "view_flows",
            "view_tools",
            "view_trackers",
            "view_projects",
            "view_issues",
            "view_compliance",
        ],
    },
}


def create_permissions(db: Session) -> Dict[str, Permission]:
    """Create all system permissions.

    Args:
        db: Database session

    Returns:
        Dictionary mapping permission names to Permission objects
    """
    permissions = {}

    for category, perms in SYSTEM_PERMISSIONS.items():
        for perm_data in perms:
            # Check if permission already exists
            existing = (
                db.query(Permission)
                .filter(Permission.name == perm_data["name"])
                .first()
            )

            if existing:
                logger.info(f"Permission '{perm_data['name']}' already exists")
                permissions[perm_data["name"]] = existing
            else:
                # Create new permission
                permission = Permission(
                    name=perm_data["name"],
                    description=perm_data["description"],
                    category=category,
                    is_active=True,
                )
                db.add(permission)
                db.flush()  # Flush to get the ID
                permissions[perm_data["name"]] = permission
                logger.info(f"Created permission: {perm_data['name']}")

    db.commit()
    return permissions


def create_system_roles(
    db: Session, permissions: Dict[str, Permission]
) -> Dict[str, Role]:
    """Create all system roles and assign permissions.

    Args:
        db: Database session
        permissions: Dictionary of permission names to Permission objects

    Returns:
        Dictionary mapping role names to Role objects
    """
    roles = {}

    for role_name, role_data in SYSTEM_ROLES.items():
        # Check if role already exists
        existing = (
            db.query(Role).filter(Role.name == role_name, Role.is_system_role).first()
        )

        if existing:
            logger.info(f"System role '{role_name}' already exists")
            role = existing

            # Update description if changed
            if role.description != role_data["description"]:
                role.description = role_data["description"]
                logger.info(f"Updated description for role: {role_name}")

            # Get existing permissions for this role
            existing_perms = {rp.permission.name for rp in role.role_permissions}
            required_perms = set(role_data["permissions"])

            # Add missing permissions
            missing_perms = required_perms - existing_perms
            for perm_name in missing_perms:
                if perm_name in permissions:
                    role_perm = RolePermission(
                        role_id=role.id, permission_id=permissions[perm_name].id
                    )
                    db.add(role_perm)
                    logger.info(f"Added permission '{perm_name}' to role '{role_name}'")

            # Remove extra permissions
            extra_perms = existing_perms - required_perms
            for perm_name in extra_perms:
                role_perm = (
                    db.query(RolePermission)
                    .join(Permission)
                    .filter(
                        RolePermission.role_id == role.id,
                        Permission.name == perm_name,
                    )
                    .first()
                )
                if role_perm:
                    db.delete(role_perm)
                    logger.info(
                        f"Removed permission '{perm_name}' from role '{role_name}'"
                    )

        else:
            # Create new role
            role = Role(
                name=role_name,
                description=role_data["description"],
                is_system_role=True,
                account_id=None,  # System roles are not account-specific
            )
            db.add(role)
            db.flush()  # Flush to get the ID

            # Add permissions to role
            for perm_name in role_data["permissions"]:
                if perm_name in permissions:
                    role_perm = RolePermission(
                        role_id=role.id, permission_id=permissions[perm_name].id
                    )
                    db.add(role_perm)
                else:
                    logger.warning(
                        f"Permission '{perm_name}' not found for role '{role_name}'"
                    )

            logger.info(
                f"Created system role: {role_name} with {len(role_data['permissions'])} permissions"
            )

        roles[role_name] = role

    db.commit()
    return roles


def initialize_system_roles(db: Session) -> bool:
    """Initialize system roles and permissions.

    This is the main entry point for initializing the RBAC system.

    Args:
        db: Database session

    Returns:
        True if successful, False otherwise
    """
    try:
        logger.info("Initializing system permissions...")
        permissions = create_permissions(db)
        logger.info(f"Created/verified {len(permissions)} permissions")

        logger.info("Initializing system roles...")
        roles = create_system_roles(db, permissions)
        logger.info(f"Created/verified {len(roles)} system roles")

        logger.info("System roles and permissions initialization complete!")
        return True

    except Exception as e:
        logger.error(f"Error initializing system roles: {e}", exc_info=True)
        db.rollback()
        return False


def main():
    """Main entry point for running the script standalone."""
    import sys

    from preloop.models.db.session import get_db_session

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Get database session
    db_gen = get_db_session()
    db = next(db_gen)

    try:
        success = initialize_system_roles(db)
        sys.exit(0 if success else 1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
