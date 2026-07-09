"""Tests for OSS permissions fallback behavior.

Ensures that the no-op require_permission decorator in OSS builds
preserves FastAPI's sync/async dispatch behavior, and that the EE
wrapper fails closed when auth dependencies are missing.
"""

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def _create_fallback_decorator():
    """Create the fallback require_permission decorator directly.

    This avoids import issues with the proprietary module symlink.
    """

    def require_permission(permission_name: str):
        """No-op permission decorator for OSS builds."""

        def decorator(func):
            return func

        return decorator

    return require_permission


class TestRequirePermissionFallback:
    """Tests for the OSS require_permission fallback decorator."""

    def test_sync_function_stays_sync(self):
        """Test that sync functions remain sync after decoration.

        This is critical for FastAPI to dispatch them to the threadpool.
        """
        require_permission = _create_fallback_decorator()

        @require_permission("test.permission")
        def sync_handler():
            return "sync result"

        # The decorated function should NOT be a coroutine function
        assert not asyncio.iscoroutinefunction(sync_handler), (
            "Sync handler should remain sync for FastAPI threadpool dispatch"
        )

        # Should be callable and return the expected result
        result = sync_handler()
        assert result == "sync result"

    def test_async_function_stays_async(self):
        """Test that async functions remain async after decoration."""
        require_permission = _create_fallback_decorator()

        @require_permission("test.permission")
        async def async_handler():
            return "async result"

        # The decorated function should still be a coroutine function
        assert asyncio.iscoroutinefunction(async_handler), (
            "Async handler should remain async"
        )

        # Should be callable and return the expected result
        result = asyncio.run(async_handler())
        assert result == "async result"

    def test_function_signature_preserved(self):
        """Test that function signature is preserved for FastAPI dependency injection."""
        require_permission = _create_fallback_decorator()

        @require_permission("test.permission")
        def handler_with_params(db, user_id: str, limit: int = 10):
            return f"user={user_id}, limit={limit}"

        # Check signature is preserved
        sig = inspect.signature(handler_with_params)
        params = list(sig.parameters.keys())
        assert "db" in params
        assert "user_id" in params
        assert "limit" in params

    def test_decorator_is_noop(self):
        """Test that the fallback decorator returns the original function unchanged."""
        require_permission = _create_fallback_decorator()

        def original_func():
            pass

        decorated = require_permission("test.permission")(original_func)

        # Should be the exact same function object
        assert decorated is original_func, (
            "Fallback should return original function unchanged"
        )

    def test_fallback_matches_implementation(self):
        """Verify the test fallback matches the actual implementation in permissions.py."""
        # Read the actual implementation
        from pathlib import Path

        perms_file = (
            Path(__file__).parent.parent / "preloop" / "utils" / "permissions.py"
        )
        content = perms_file.read_text()

        # Verify the fallback returns the function unchanged
        assert "return func" in content, "Fallback should return func unchanged"
        # Verify OSS path does not create a bare `async def wrapper`
        assert "async def wrapper" not in content, (
            "Fallback should not create async wrapper"
        )


class TestRequirePermissionMissingDependencies:
    """Fail closed when current_user/db are absent from kwargs."""

    def test_ensure_permission_dependencies_raises_without_kwargs(self):
        from preloop.utils.permissions import _ensure_permission_dependencies

        with pytest.raises(HTTPException) as exc_info:
            _ensure_permission_dependencies()

        assert exc_info.value.status_code == 500
        assert "current_user and db" in exc_info.value.detail

    def test_ensure_permission_dependencies_raises_when_partial(self):
        from preloop.utils.permissions import _ensure_permission_dependencies

        with pytest.raises(HTTPException) as exc_info:
            _ensure_permission_dependencies(current_user=MagicMock())

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

        @perms.require_permission("view_issues")
        async def async_handler():
            called["raw"] = True
            return "ok"

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(async_handler())

        assert exc_info.value.status_code == 500
        assert called["plugin"] is False
        assert called["raw"] is False
