"""Profile permissions enforce basic roles and preserve commercial overlays."""

from unittest.mock import MagicMock

import pytest

from preloop.api.auth import router as auth_router
from preloop.config import settings
from preloop.utils import permissions


def test_resolve_user_permissions_disable_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "disable_rbac", True)
    assert auth_router._resolve_user_permissions(MagicMock(), MagicMock()) is None


def test_resolve_user_permissions_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "disable_rbac", False)
    resolver = MagicMock(return_value=["view_issues"])
    monkeypatch.setattr(permissions, "user_permission_names", resolver)
    user, db = MagicMock(), MagicMock()
    assert auth_router._resolve_user_permissions(user, db) == ["view_issues"]
    resolver.assert_called_once_with(db, user)


def test_resolve_user_permissions_failure_denies_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(
        permissions,
        "user_permission_names",
        MagicMock(side_effect=RuntimeError("unavailable")),
    )
    assert auth_router._resolve_user_permissions(MagicMock(), MagicMock()) == []


def test_resolve_user_permissions_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    from preloop.plugins import base

    monkeypatch.setattr(settings, "disable_rbac", False)
    manager = MagicMock()
    resolver = MagicMock(return_value=["view_issues"])
    manager.get_service.return_value = resolver
    monkeypatch.setattr(base, "get_plugin_manager", lambda: manager)
    user, db = MagicMock(), MagicMock()
    assert auth_router._resolve_user_permissions(user, db) == ["view_issues"]
    resolver.assert_called_once_with(user, db)
