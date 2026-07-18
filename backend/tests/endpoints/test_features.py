"""Tests for features endpoint."""

from unittest.mock import MagicMock, patch


class TestGetFeatures:
    """Test get_features endpoint."""

    @patch("preloop.api.endpoints.features.crud_user")
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
                "session_optimization": True,
            },
        }
        mock_get_plugin_manager.assert_called_once()
        mock_plugin_manager.get_enabled_features.assert_called_once()

    @patch("preloop.api.endpoints.features.crud_user")
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
                "session_optimization": True,
            },
        }

    @patch("preloop.api.endpoints.features.crud_user")
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
        assert len(result["features"]) == 7
        assert result["features"]["session_optimization"] is True

    @patch("preloop.api.endpoints.features.crud_user")
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

    @patch("preloop.api.endpoints.features.crud_user")
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

    @patch("preloop.api.endpoints.features.settings")
    @patch("preloop.api.endpoints.features.crud_user")
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

        result = get_features(db=MagicMock())

        assert result["features"]["first_account_pending"] is False
        assert result["features"]["registration"] is False
