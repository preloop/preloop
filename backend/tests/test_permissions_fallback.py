"""Tests for OSS permissions fallback behavior.

Ensures that the require_permission decorator in OSS builds
preserves FastAPI's sync/async dispatch behavior, and that the EE
wrapper fails closed when auth dependencies are missing.
"""

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


class TestRequirePermissionFallback:
    """The actual core fallback preserves dispatch and denies missing dependencies."""

    def test_sync_function_stays_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from preloop.utils import permissions

        monkeypatch.setattr(permissions, "_plugin_require_permission", None)

        @permissions.require_permission("view_issues")
        def handler():
            return "sync result"

        assert not inspect.iscoroutinefunction(handler)
        assert handler() == "sync result"

    def test_async_function_stays_async(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from preloop.utils import permissions

        monkeypatch.setattr(permissions, "_plugin_require_permission", None)

        @permissions.require_permission("view_issues")
        async def handler():
            return "async result"

        assert inspect.iscoroutinefunction(handler)
        assert asyncio.run(handler()) == "async result"

    def test_missing_dependencies_fail_closed_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from preloop.utils import permissions

        monkeypatch.setattr(permissions, "_plugin_require_permission", None)
        monkeypatch.setattr(permissions, "_rbac_checks_enabled", lambda: True)

        @permissions.require_permission("manage_users")
        def handler():
            pytest.fail("Unauthorized endpoint body was reached")

        with pytest.raises(HTTPException) as error:
            handler()
        assert error.value.status_code == 500


class TestRequirePermissionMissingDependencies:
    """Fail closed when current_user/db are absent from kwargs."""

    def test_ensure_permission_dependencies_raises_without_kwargs(self):
        import preloop.utils.permissions as perms

        with pytest.raises(HTTPException) as exc_info:
            perms._ensure_permission_dependencies()

        assert exc_info.value.status_code == 500
        assert "current_user and db" in exc_info.value.detail

    def test_ensure_permission_dependencies_raises_when_partial(self):
        import preloop.utils.permissions as perms

        with pytest.raises(HTTPException) as exc_info:
            perms._ensure_permission_dependencies(current_user=MagicMock())

        assert exc_info.value.status_code == 500

    def test_sync_wrapper_does_not_bypass_without_deps(self, monkeypatch):
        import preloop.utils.permissions as perms

        called = {"plugin": False, "raw": False}

        def fake_plugin_require(permission_name: str):
            def decorator(func):
                async def plugin_wrapped(*args, **kwargs):
                    called["plugin"] = True
                    return await func(*args, **kwargs)

                return plugin_wrapped

            return decorator

        monkeypatch.setattr(perms, "_plugin_require_permission", fake_plugin_require)
        # Force the fail-closed path even under the suite's DISABLE_RBAC default.
        monkeypatch.setattr(perms, "_rbac_checks_enabled", lambda: True)

        @perms.require_permission("view_issues")
        def sync_handler():
            called["raw"] = True
            return "ok"

        with pytest.raises(HTTPException) as exc_info:
            sync_handler()

        assert exc_info.value.status_code == 500
        assert called["plugin"] is False
        assert called["raw"] is False

    def test_async_wrapper_does_not_bypass_without_deps(self, monkeypatch):
        import preloop.utils.permissions as perms

        called = {"plugin": False, "raw": False}

        def fake_plugin_require(permission_name: str):
            def decorator(func):
                async def plugin_wrapped(*args, **kwargs):
                    called["plugin"] = True
                    return await func(*args, **kwargs)

                return plugin_wrapped

            return decorator

        monkeypatch.setattr(perms, "_plugin_require_permission", fake_plugin_require)
        monkeypatch.setattr(perms, "_rbac_checks_enabled", lambda: True)

        @perms.require_permission("view_issues")
        async def async_handler():
            called["raw"] = True
            return "ok"

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(async_handler())

        assert exc_info.value.status_code == 500
        assert called["plugin"] is False
        assert called["raw"] is False

    def test_wrapper_skips_deps_check_when_rbac_disabled(self, monkeypatch):
        """Under DISABLE_RBAC the fail-closed deps gate must not fire."""
        import preloop.utils.permissions as perms

        def fake_plugin_require(permission_name: str):
            def decorator(func):
                def plugin_wrapped(*args, **kwargs):
                    return func(*args, **kwargs)

                return plugin_wrapped

            return decorator

        monkeypatch.setattr(perms, "_plugin_require_permission", fake_plugin_require)
        monkeypatch.setattr(perms, "_rbac_checks_enabled", lambda: False)

        @perms.require_permission("view_issues")
        def sync_handler():
            return "ok"

        assert sync_handler() == "ok"
