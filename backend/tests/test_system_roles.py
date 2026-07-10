"""Tests for seeded system roles and enforced permission names."""

from __future__ import annotations

import importlib.util
import ast
from pathlib import Path


def _load_system_roles_module():
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "scripts" / "init_system_roles.py",
        here.parents[3] / "preloop" / "scripts" / "init_system_roles.py",
    ]
    module_path = next((path for path in candidates if path.exists()), None)
    assert module_path is not None, "Could not locate init_system_roles.py"
    spec = importlib.util.spec_from_file_location("init_system_roles", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SYSTEM_ROLES_MODULE = _load_system_roles_module()
SYSTEM_PERMISSIONS = SYSTEM_ROLES_MODULE.SYSTEM_PERMISSIONS
SYSTEM_ROLES = SYSTEM_ROLES_MODULE.SYSTEM_ROLES


def _repo_roots() -> list[Path]:
    here = Path(__file__).resolve()
    roots = [here.parents[2]]
    ee_plugins = here.parents[3] / "plugins"
    if ee_plugins.is_dir():
        roots.append(ee_plugins)
    return roots


def _permission_names() -> set[str]:
    return {
        permission["name"]
        for category_permissions in SYSTEM_PERMISSIONS.values()
        for permission in category_permissions
    }


def _role_permissions(role_name: str) -> set[str]:
    return set(SYSTEM_ROLES[role_name]["permissions"])


def test_system_role_matrix_matches_permission_vocabulary() -> None:
    permission_names = _permission_names()

    for role in SYSTEM_ROLES.values():
        assert set(role["permissions"]) <= permission_names

    assert _role_permissions("owner") == permission_names
    assert "close_account" not in _role_permissions("admin")
    assert "manage_billing" not in _role_permissions("admin")

    viewer_permissions = _role_permissions("viewer")
    disallowed_prefixes = (
        "manage_",
        "create_",
        "edit_",
        "delete_",
        "execute_",
        "decide_",
        "control_",
    )
    assert all(
        not permission.startswith(disallowed_prefixes)
        for permission in viewer_permissions
    )

    assert "create_projects" in _role_permissions("editor")
    assert "manage_projects" not in _role_permissions("editor")
    assert "view_audit_logs" in _role_permissions("analyst")
    assert "view_audit_logs" in _role_permissions("admin")

    for role_name in ("analyst", "executor", "editor", "admin"):
        assert "view_cost" in _role_permissions(role_name)

    assert "manage_budgets" in _role_permissions("admin")
    assert "manage_budgets" in _role_permissions("owner")
    assert "manage_budgets" not in _role_permissions("editor")
    assert "manage_budgets" not in _role_permissions("analyst")

    for role_name in ("executor", "editor", "admin", "owner"):
        assert "decide_approvals" in _role_permissions(role_name)


def test_known_permission_names_are_seeded() -> None:
    permission_names = _permission_names()
    known_required_permissions = {
        "view_ai_models",
        "view_audit_logs",
        "view_admin_dashboard",
        "create_projects",
        "create_trackers",
        "create_teams",
        "manage_budgets",
        "view_cost",
        "view_approvals",
        "decide_approvals",
        "view_agents",
        "view_runtime_sessions",
        "view_policies",
        "manage_policies",
    }

    assert known_required_permissions <= permission_names


def test_enforced_permission_names_are_seeded() -> None:
    permission_names = _permission_names()
    ignored_parts = {"tests", "docs", "guide", "node_modules", "__pycache__"}

    enforced_permissions: set[str] = set()
    for root in _repo_roots():
        for path in root.rglob("*.py"):
            relative_parts = set(path.relative_to(root).parts)
            if relative_parts & ignored_parts:
                continue
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue

            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    func = decorator.func
                    is_require_permission = (
                        isinstance(func, ast.Name) and func.id == "require_permission"
                    ) or (
                        isinstance(func, ast.Attribute)
                        and func.attr == "require_permission"
                    )
                    if not is_require_permission or not decorator.args:
                        continue
                    permission_arg = decorator.args[0]
                    if isinstance(permission_arg, ast.Constant) and isinstance(
                        permission_arg.value, str
                    ):
                        enforced_permissions.add(permission_arg.value)

    assert enforced_permissions <= permission_names
