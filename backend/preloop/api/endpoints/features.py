"""System features and plugin detection endpoints."""

from typing import Any, Dict

from fastapi import APIRouter

from preloop.config import settings
from preloop.plugins.base import get_plugin_manager

router = APIRouter()


@router.get("/features")
def get_features() -> Dict[str, Any]:
    """Get enabled features and plugins.

    Returns information about which plugins are installed and what features
    are available in the system. This allows the frontend to dynamically
    show/hide UI sections based on backend capabilities.

    Returns:
        Dictionary with:
        - plugins: List of enabled plugin metadata
        - features: Dict of feature flags (e.g., rbac, user_management, registration, etc.)
    """
    plugin_manager = get_plugin_manager()
    result = plugin_manager.get_enabled_features()

    # Add config-based feature flags
    result["features"]["registration"] = settings.registration_enabled

    # Session optimization ships in the open-source core (0.12.0): the
    # capability is always present, so the console must always show it.
    # Deployments that meter hosted-model analysis gate at request time via
    # the optimization_gating authorizer (402), never by hiding the UI.
    # setdefault so a plugin that already set the flag keeps its value.
    result["features"].setdefault("session_optimization", True)

    return result
