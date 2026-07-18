"""System features and plugin detection endpoints."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_user
from preloop.models.db.session import get_db_session
from preloop.plugins.base import get_plugin_manager

router = APIRouter()

# Sticky process-level cache for "does this instance have any users?".
# Transitions only once in an instance lifetime (False → True when the first
# account registers) and never flips back, so caching True forever is safe and
# avoids a DB round-trip on every /features call (landing, signup, authed views).
# None = unknown (must query); True = at least one user exists.
_users_exist_cache: Optional[bool] = None


def reset_users_exist_cache() -> None:
    """Clear the sticky users-exist cache (for tests)."""
    global _users_exist_cache
    _users_exist_cache = None


def _first_account_pending(db: Session) -> bool:
    """Return True when signup would create the instance's first admin account.

    Uses a sticky process-level cache once any user is known to exist so the
    common case (production after first signup) never hits the DB again.
    """
    global _users_exist_cache
    if not settings.registration_enabled:
        return False
    if _users_exist_cache is True:
        return False
    if crud_user.has_any_users(db):
        _users_exist_cache = True
        return False
    return True


@router.get("/features")
def get_features(db: Session = Depends(get_db_session)) -> Dict[str, Any]:
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

    # First-account context for the signup form: when no user exists yet the
    # console explains that the account being created becomes the admin
    # account. Only meaningful while registration is open. Cached process-
    # wide once the first user exists (never flips back).
    result["features"]["first_account_pending"] = _first_account_pending(db)

    # Session optimization ships in the open-source core (0.12.0): the
    # capability is always present, so the console must always show it.
    # Deployments that meter hosted-model analysis gate at request time via
    # the optimization_gating authorizer (402), never by hiding the UI.
    # setdefault so a plugin that already set the flag keeps its value.
    result["features"].setdefault("session_optimization", True)

    return result
