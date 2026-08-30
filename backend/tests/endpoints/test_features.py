"""Tests for features endpoint."""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_users_exist_cache():
    """Each test starts with an empty sticky users-exist cache."""
    from preloop.api.endpoints.features import reset_users_exist_cache

    reset_users_exist_cache()
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
            "plugins": ["rbac", "audit"],
            "features": {
                "rbac": True,
                "audit_logging": True,
                "registration": True,
                "first_account_pending": False,
                "registration_bootstrap_pending": False,
                "session_optimization": True,
                "policies_console": False,
                "passkeys": True,
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
            "plugins": [],
            "features": {
                "registration": True,
                "first_account_pending": False,
                "registration_bootstrap_pending": False,
                "session_optimization": True,
                "policies_console": False,
                "passkeys": True,
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
        assert len(result["features"]) == 10
        assert result["features"]["session_optimization"] is True

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
    def test_policies_console_off_by_default(
        self, mock_get_plugin_manager, mock_crud_user
    ):
        """The Policies console page is hidden unless an operator opts in."""
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

        assert result["features"]["policies_console"] is False

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

    @patch("preloop.api.auth.bootstrap.crud_user")
    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_policies_console_respects_plugin_value(
        self, mock_get_plugin_manager, mock_crud_user
    ):
        """setdefault semantics: a plugin that already set the flag wins."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"policies_console": True},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        mock_crud_user.has_any_users.return_value = True

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRELOOP_POLICIES_CONSOLE", None)
            result = get_features(db=MagicMock())

        assert result["features"]["policies_console"] is True

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
