"""OSS edition boundaries do not depend on commercial controllers."""

import os
from pathlib import Path
import subprocess
import sys


def test_basic_routes_boot_without_proprietary_plugins() -> None:
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
        DISABLE_PROPRIETARY_PLUGINS="true",
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
