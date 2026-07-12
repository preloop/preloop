"""Tests for utils.permissions permission decorator behavior."""

import asyncio
from unittest.mock import MagicMock

from preloop.utils import permissions


class TestRequirePermissionOSSFallback:
    """Test require_permission decorator (OSS build uses no-op fallback).

    When the proprietary RBAC plugin is present (EE), the fail-closed wrapper
    still requires ``current_user`` and ``db`` kwargs. These tests pass dummy
    dependencies so that path can be exercised without weakening production
    fail-closed behavior. With DISABLE_RBAC=true (set in tests/conftest.py),
    the plugin allows the call through.
    """

    def test_require_permission_preserves_sync_function(self):
        """require_permission decorator preserves sync function behavior."""

        @permissions.require_permission("admin:read")
        def my_sync_func(x: int, *, current_user=None, db=None) -> int:
            return x + 1

        assert my_sync_func(41, current_user=MagicMock(), db=MagicMock()) == 42

    def test_require_permission_preserves_async_function(self):
        """require_permission decorator preserves async function for FastAPI."""

        @permissions.require_permission("admin:write")
        async def my_async_func(x: int, *, current_user=None, db=None) -> int:
            return x * 2

        result = asyncio.run(
            my_async_func(21, current_user=MagicMock(), db=MagicMock())
        )
        assert result == 42

    def test_require_permission_with_different_permission_names(self):
        """Decorator accepts various permission names."""

        @permissions.require_permission("flows:create")
        def create_flow(*, current_user=None, db=None):
            return "created"

        assert create_flow(current_user=MagicMock(), db=MagicMock()) == "created"

    def test_require_permission_sync_wrapper_supports_running_event_loop(
        self, monkeypatch
    ):
        """Sync endpoints should still work when the current thread already has a loop."""

        def fake_plugin_require_permission(_permission_name: str):
            def decorator(func):
                async def wrapper(*args, **kwargs):
                    return func(*args, **kwargs)

                return wrapper

            return decorator

        monkeypatch.setattr(
            permissions, "_plugin_require_permission", fake_plugin_require_permission
        )

        @permissions.require_permission("flows:create")
        def sync_handler(*, current_user, db):
            return "ok"

        async def run_test():
            return sync_handler(current_user=MagicMock(), db=MagicMock())

        assert asyncio.run(run_test()) == "ok"
