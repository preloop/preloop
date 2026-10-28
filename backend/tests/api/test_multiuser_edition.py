"""OSS edition boundaries do not depend on commercial controllers."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("disabled", ["true", "1", "yes"])
def test_basic_routes_boot_without_proprietary_plugins(disabled: str) -> None:
    """Create the OSS app and OpenAPI schema in a clean process."""
    script = """
import sys
from preloop.api.app import create_app
from preloop.plugins.base import get_plugin_manager
app = create_app()
paths = app.openapi()["paths"]
for path in ("/api/v1/users", "/api/v1/teams", "/api/v1/invitations", "/api/v1/invitations/public/{token}", "/api/v1/invitations/accept"):
    assert path in paths, path
assert "/api/v1/roles" not in paths
assert "/api/v1/teams/{team_id}/roles" not in paths
assert "/api/v1/users/{user_id}/roles" not in paths
assert "/api/v1/users/{user_id}/roles/{role_id}" not in paths
features = get_plugin_manager().get_enabled_features()["features"]
assert features["user_management"] and features["team_management"]
assert "basic_roles" not in features
assert not features.get("role_management")
assert not features.get("rbac")
assert not features.get("advanced_approvals")
assert not features.get("team_budgets")
assert not any(name.startswith("preloop.plugins.proprietary.") for name in sys.modules)
"""
    environment = dict(
        os.environ,
        PRELOOP_DISABLE_TELEMETRY="true",
        TESTING="true",
        DISABLE_PROPRIETARY_PLUGINS=disabled,
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "missing", ["preloop.plugins.proprietary", "preloop.plugins.proprietary.rbac"]
)
def test_absent_commercial_package_is_core(missing: str) -> None:
    """A distribution without the optional package remains unrestricted core."""
    _check_rbac_import(missing, expect_core=True)


@pytest.mark.parametrize(
    "missing",
    ["commercial_dependency", "preloop.plugins.proprietary.rbac.permissions", None],
)
def test_broken_commercial_import_refuses_startup(missing: str | None) -> None:
    """An installed authorization dependency failure must not grant full access."""
    _check_rbac_import(missing, expect_core=False)


def _check_rbac_import(missing: str | None, *, expect_core: bool) -> None:
    script = f"""
import builtins
original = builtins.__import__
def importing(name, *args, **kwargs):
    if name == "preloop.plugins.proprietary.rbac.permissions":
        if {missing!r} is None:
            raise ImportError("broken commercial authorization")
        raise ModuleNotFoundError("missing authorization dependency", name={missing!r})
    return original(name, *args, **kwargs)
builtins.__import__ = importing
try:
    from preloop.utils.permissions import members_have_full_access
except ImportError:
    assert not {expect_core!r}
else:
    assert {expect_core!r}, "broken RBAC became unrestricted core"
    assert members_have_full_access()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(
            os.environ,
            PRELOOP_DISABLE_TELEMETRY="true",
            TESTING="true",
            DISABLE_PROPRIETARY_PLUGINS="false",
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("disabled", ["true", "1", "yes"])
def test_loading_honors_every_authorization_disable_value(
    monkeypatch: pytest.MonkeyPatch, disabled: str
) -> None:
    from preloop.config import settings
    from preloop.plugins import base

    discovered: list[str] = []
    monkeypatch.setenv("DISABLE_PROPRIETARY_PLUGINS", disabled)
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(base, "_plugin_manager", None)
    monkeypatch.setattr(
        base.PluginManager,
        "discover_plugins",
        lambda self, name: discovered.append(name),
    )
    base.get_plugin_manager()
    assert discovered == ["preloop.plugins.builtin"]
