"""Tests for features endpoint."""

from unittest.mock import MagicMock, patch


class TestGetFeatures:
    """Test get_features endpoint."""

    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_get_features_success(self, mock_get_plugin_manager):
        """Test getting features successfully."""
        from preloop.api.endpoints.features import get_features

        # Mock plugin manager
        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": ["rbac", "audit"],
            "features": {"rbac": True, "audit_logging": True, "registration": True},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager

        result = get_features()

        assert result == {
            "plugins": ["rbac", "audit"],
            "features": {
                "rbac": True,
                "audit_logging": True,
                "registration": True,
                "session_optimization": True,
            },
        }
        mock_get_plugin_manager.assert_called_once()
        mock_plugin_manager.get_enabled_features.assert_called_once()

    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_get_features_empty_plugins(self, mock_get_plugin_manager):
        """Test getting features when no plugins are enabled."""
        from preloop.api.endpoints.features import get_features

        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"registration": True},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager

        result = get_features()

        assert result == {
            "plugins": [],
            "features": {"registration": True, "session_optimization": True},
        }

    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_get_features_with_multiple_plugins(self, mock_get_plugin_manager):
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

        result = get_features()

        assert "plugins" in result
        assert "features" in result
        assert len(result["plugins"]) == 3
        assert len(result["features"]) == 6
        assert result["features"]["session_optimization"] is True

    @patch("preloop.api.endpoints.features.get_plugin_manager")
    def test_session_optimization_always_advertised(self, mock_get_plugin_manager):
        """The OSS core ships session optimization (0.12.x): the flag is
        always present so the console shows the Optimize tab; a plugin that
        explicitly set it keeps its value (setdefault semantics)."""
        mock_plugin_manager = MagicMock()
        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {},
        }
        mock_get_plugin_manager.return_value = mock_plugin_manager
        from preloop.api.endpoints.features import get_features

        assert get_features()["features"]["session_optimization"] is True

        mock_plugin_manager.get_enabled_features.return_value = {
            "plugins": [],
            "features": {"session_optimization": False},
        }
        assert get_features()["features"]["session_optimization"] is False
