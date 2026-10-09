"""Tests for features endpoint."""

import os
from unittest.mock import MagicMock, patch

import pytest

from preloop.config import SERVER_VERSION


@pytest.fixture(autouse=True)
def _clear_users_exist_cache():
    """Each test starts with an empty sticky users-exist cache."""
    from preloop.api.endpoints.features import reset_users_exist_cache

    reset_users_exist_cache()
    with (
        patch(
            "preloop.services.instance_service.is_hosted_instance", return_value=False
        ),
        patch("preloop.services.instance_service._is_enterprise", return_value=False),
    ):
        yield
    reset_users_exist_cache()


class TestGetFeatures:
    """Test get_features endpoint."""

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_get_features_success(self, mock_get_plugin_manager, mock_crud_user):
        """Test getting features successfully."""
        from preloop.api.endpoints.features import get_features

        # Mock plugin manager
        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": ["rbac", "audit"],
            "features": {"rbac": True, "audit_logging": True, "registration": True},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        result = get_features(db=MagicMock())

        assert result == {
            "edition": "oss",
            "server_version": SERVER_VERSION,
            "plugins": ["rbac", "audit"],
            "features": {
                "rbac": True,
                "audit_logging": True,
                "registration": True,
                "first_account_pending": False,
                "registration_bootstrap_pending": False,
                "session_optimization": True,
                "chat_connections": True,
                "policies_console": True,
                "bitbucket_dc": False,
                "passkeys": True,
                "multi_account": False,
                "account_hierarchy": False,
                "abac_rules": False,
            },
        }
        mock_get_plugin_manager.assert_called_once()
        mock_plugin_manager.get_enabled_features.assert_called_once()

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_get_features_empty_plugins(self, mock_get_plugin_manager, mock_crud_user):
        """Test getting features when no plugins are enabled."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"registration": True},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        result = get_features(db=MagicMock())

        assert result == {
            "edition": "oss",
            "server_version": SERVER_VERSION,
            "plugins": [],
            "features": {
                "registration": True,
                "first_account_pending": False,
                "registration_bootstrap_pending": False,
                "session_optimization": True,
                "chat_connections": True,
                "policies_console": True,
                "bitbucket_dc": False,
                "passkeys": True,
                "multi_account": False,
                "account_hierarchy": False,
                "abac_rules": False,
            },
        }

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_get_features_with_multiple_plugins(
        self, mock_get_plugin_manager, mock_crud_user
    ):
        """Test getting features with multiple plugins."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": ["rbac", "audit", "compliance"],
            "features": {
                "rbac": True,
                "audit_logging": True,
                "compliance_metrics": True,
                "custom_workflows": False,
                "registration": True,
            },
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        result = get_features(db=MagicMock())

        assert "plugins" in result
        assert "features" in result
        assert len(result["plugins"]) == 3
        assert len(result["features"]) == 15
        assert result["features"]["session_optimization"] is True
        assert result["features"]["chat_connections"] is True

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_session_optimization_always_advertised(
        self, mock_get_plugin_manager, mock_crud_user
    ):
        """The OSS core ships session optimization (0.12.x): the flag is
        always present so the console shows the Optimize tab; a plugin that
        explicitly set it keeps its value (setdefault semantics)."""
        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True
        from preloop.api.endpoints.features import get_features

        assert get_features(db=MagicMock())["features"]["session_optimization"] is True

        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"session_optimization": False},
        }
        assert get_features(db=MagicMock())["features"]["session_optimization"] is False

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_policies_console_on_by_default(
        self, mock_get_plugin_manager, mock_crud_user
    ):
        """Regular users can discover Policies without an operator opting in."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRELOOP_POLICIES_CONSOLE", None)
            result = get_features(db=MagicMock())

        assert result["features"]["policies_console"] is True

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", " Yes "])
    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_policies_console_env_override_enables(
        self, mock_get_plugin_manager, mock_crud_user, value
    ):
        """PRELOOP_POLICIES_CONSOLE opts the page in, case and space tolerant."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        with patch.dict(os.environ, {"PRELOOP_POLICIES_CONSOLE": value}):
            result = get_features(db=MagicMock())

        assert result["features"]["policies_console"] is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " No ", ""])
    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_policies_console_env_override_disables(
        self, mock_get_plugin_manager, mock_crud_user, value
    ):
        """PRELOOP_POLICIES_CONSOLE opts the page out, case and space tolerant."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        with patch.dict(os.environ, {"PRELOOP_POLICIES_CONSOLE": value}):
            result = get_features(db=MagicMock())

        assert result["features"]["policies_console"] is False

    @pytest.mark.parametrize("enabled", [True, False])
    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_policies_console_respects_plugin_value(
        self, mock_get_plugin_manager, mock_crud_user, enabled
    ):
        """setdefault semantics: a plugin that already set the flag wins."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"policies_console": enabled},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRELOOP_POLICIES_CONSOLE", None)
            result = get_features(db=MagicMock())

        assert result["features"]["policies_console"] is enabled

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_first_account_pending_on_fresh_instance(
        self, mock_get_plugin_manager, mock_crud_user
    ):
        """With registration open and zero users the signup form gets the
        first-account context flag."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = False

        result = get_features(db=MagicMock())

        assert result["features"]["first_account_pending"] is True
        # No bootstrap token configured: not pending.
        assert result["features"]["registration_bootstrap_pending"] is False

    @patch("preloop.api.auth.bootstrap.settings")
    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_first_account_pending_requires_open_registration(
        self, mock_get_plugin_manager, mock_crud_user, mock_settings
    ):
        """With registration disabled the flag stays False even at zero users."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = False
        mock_settings.registration_enabled = False
        mock_settings.bootstrap_token = ""

        result = get_features(db=MagicMock())

        assert result["features"]["first_account_pending"] is False
        assert result["features"]["registration"] is False
        assert result["features"]["registration_bootstrap_pending"] is False
        # Registration closed and no bootstrap token: no need to query the
        # users table.
        mock_crud_user.has_any_users.assert_not_called()

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_first_account_pending_sticky_cache_skips_db_after_users_exist(
        self, mock_get_plugin_manager, mock_crud_user
    ):
        """Once any user exists, subsequent /features calls skip the DB query."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        assert (
            get_features(db=MagicMock())["features"]["first_account_pending"] is False
        )
        assert mock_crud_user.has_any_users.call_count == 1

        # Second call: sticky cache must avoid another has_any_users hit.
        assert (
            get_features(db=MagicMock())["features"]["first_account_pending"] is False
        )
        assert mock_crud_user.has_any_users.call_count == 1

    @patch("preloop.api.auth.bootstrap.settings")
    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_bootstrap_pending_on_unclaimed_instance(
        self, mock_get_plugin_manager, mock_crud_user, mock_settings
    ):
        """Zero users + token configured: signup stays reachable (even with
        registration disabled) and the form is told the setup link is needed."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = False
        mock_settings.registration_enabled = False
        mock_settings.bootstrap_token = "sekret"

        result = get_features(db=MagicMock())

        assert result["features"]["registration"] is True
        assert result["features"]["registration_bootstrap_pending"] is True
        assert result["features"]["first_account_pending"] is True

    @patch("preloop.api.auth.bootstrap.settings")
    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_bootstrap_not_pending_once_claimed(
        self, mock_get_plugin_manager, mock_crud_user, mock_settings
    ):
        """Once any user exists the token is ignored: registration_enabled
        decides, and bootstrap_pending is False."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True
        mock_settings.registration_enabled = False
        mock_settings.bootstrap_token = "sekret"

        result = get_features(db=MagicMock())

        assert result["features"]["registration"] is False
        assert result["features"]["registration_bootstrap_pending"] is False
        assert result["features"]["first_account_pending"] is False


class TestAccountCapabilities:
    """The account capabilities (issue #988) default off; a plugin turns them on."""

    CAPABILITIES = ("multi_account", "account_hierarchy", "abac_rules")

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_capabilities_default_off(self, mock_get_plugin_manager, mock_crud_user):
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        features = get_features(db=MagicMock())["features"]

        for name in self.CAPABILITIES:
            assert features[name] is False, name

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_plugin_value_is_kept(self, mock_get_plugin_manager, mock_crud_user):
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [{"name": "ee"}],
            "features": {name: True for name in self.CAPABILITIES},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        features = get_features(db=MagicMock())["features"]

        for name in self.CAPABILITIES:
            assert features[name] is True, name


@pytest.mark.parametrize("enabled", [False, True])
def test_dc_feature_is_deployment_owned(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from preloop.api.endpoints.features import get_features

    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_ENABLED", "true" if enabled else "false")
    with (
        patch("preloop.api.endpoints.features.get_plugin_manager") as manager,
        patch("preloop.api.auth.bootstrap.crud_user") as users,
    ):
        manager.return_value.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"bitbucket_dc": not enabled},
        }
        users.has_any_users.return_value = True
        assert get_features(db=MagicMock())["features"]["bitbucket_dc"] is enabled


@pytest.mark.parametrize(
    "hosted,enterprise,expected",
    [
        (False, False, "oss"),
        (False, True, "enterprise"),
        (True, False, "cloud"),
        (True, True, "cloud"),
    ],
)
def test_features_edition(hosted: bool, enterprise: bool, expected: str) -> None:
    from preloop.api.endpoints.features import get_features

    with (
        patch("preloop.api.endpoints.features.get_plugin_manager") as manager,
        patch("preloop.api.auth.bootstrap.crud_user") as users,
        patch(
            "preloop.services.instance_service.is_hosted_instance", return_value=hosted
        ),
        patch(
            "preloop.services.instance_service._is_enterprise", return_value=enterprise
        ),
    ):
        manager.return_value.get_enabled_features.return_value = {
            "plugins": [{"name": "billing"}],
            "features": {},
        }
        users.has_any_users.return_value = True
        payload = get_features(db=MagicMock())
        assert payload["edition"] == expected
        assert payload["server_version"] == SERVER_VERSION


@pytest.mark.parametrize("declared", ["oss", "cloud", "enterprise", "unknown"])
def test_plugin_edition_declaration(declared: str) -> None:
    from preloop.api.endpoints.features import get_features

    with (
        patch("preloop.api.endpoints.features.get_plugin_manager") as manager,
        patch("preloop.api.auth.bootstrap.crud_user") as users,
        patch(
            "preloop.services.instance_service.is_hosted_instance", return_value=False
        ),
        patch("preloop.services.instance_service._is_enterprise", return_value=False),
    ):
        manager.return_value.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"edition": declared},
        }
        users.has_any_users.return_value = True
        assert get_features(db=MagicMock())["edition"] == "oss"


@pytest.mark.parametrize(
    "hosted,enterprise,declared,expected",
    [(True, True, "enterprise", "cloud"), (False, True, "cloud", "enterprise")],
)
def test_static_plugin_cannot_relabel_runtime_edition(
    hosted: bool, enterprise: bool, declared: str, expected: str
) -> None:
    from preloop.api.endpoints.features import get_features

    with (
        patch("preloop.api.endpoints.features.get_plugin_manager") as manager,
        patch("preloop.api.auth.bootstrap.crud_user") as users,
        patch(
            "preloop.services.instance_service.is_hosted_instance", return_value=hosted
        ),
        patch(
            "preloop.services.instance_service._is_enterprise", return_value=enterprise
        ),
    ):
        manager.return_value.get_enabled_features.return_value = {
            "plugins": [{"name": "billing"}],
            "edition": declared,
            "features": {"edition": declared},
        }
        users.has_any_users.return_value = True
        assert get_features(db=MagicMock())["edition"] == expected
