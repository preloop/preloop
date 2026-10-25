"""Unit tests for optional RBAC permission resolution on /users/me."""

import sys
from types import ModuleType
from unittest.mock import MagicMock

from preloop.api.auth import router as auth_router
from preloop.config import settings

_RBAC_PERMS_MOD = "preloop.plugins.proprietary.rbac.permissions"


def test_resolve_user_permissions_disable_rbac(monkeypatch):
    monkeypatch.setattr(settings, "disable_rbac", True)
    user = MagicMock()
    db = MagicMock()

    assert auth_router._resolve_user_permissions(user, db) is None


def test_resolve_user_permissions_import_error(monkeypatch):
    """Broken/missing RBAC deps must soft-fail (ImportError, not only ModuleNotFound)."""
    monkeypatch.setattr(settings, "disable_rbac", False)
    # None in sys.modules makes import raise ModuleNotFoundError (ImportError subclass).
    monkeypatch.setitem(sys.modules, _RBAC_PERMS_MOD, None)

    assert auth_router._resolve_user_permissions(MagicMock(), MagicMock()) is None


def test_resolve_user_permissions_db_failure_soft_fails(monkeypatch):
    monkeypatch.setattr(settings, "disable_rbac", False)

    fake_module = ModuleType(_RBAC_PERMS_MOD)

    def boom(user, db):
        raise RuntimeError("db unavailable")

    fake_module.get_user_permissions = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _RBAC_PERMS_MOD, fake_module)

    assert auth_router._resolve_user_permissions(MagicMock(), MagicMock()) is None


def test_resolve_user_permissions_success(monkeypatch):
    monkeypatch.setattr(settings, "disable_rbac", False)

    fake_module = ModuleType(_RBAC_PERMS_MOD)
    fake_module.get_user_permissions = MagicMock(return_value=["view_issues"])  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _RBAC_PERMS_MOD, fake_module)

    user = MagicMock()
    db = MagicMock()
    assert auth_router._resolve_user_permissions(user, db) == ["view_issues"]
    fake_module.get_user_permissions.assert_called_once_with(user, db)
