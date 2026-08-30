"""System features and plugin detection endpoints."""

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from preloop.api.auth.bootstrap import (
    registration_state,
    reset_users_exist_cache,
)
from preloop.models.db.session import get_db_session
from preloop.plugins.base import get_plugin_manager

__all__ = [
    "router",
    "get_features",
    "reset_users_exist_cache",
    "policies_console_enabled",
]

router = APIRouter()


def policies_console_enabled() -> bool:
    """Whether the Policies console page is exposed (default off).

    The page is still under construction, so it ships hidden. Operators opt in
    with ``PRELOOP_POLICIES_CONSOLE=true|1|yes|on``. Instance admins see the
    page regardless: that check lives in the console, not here.

    Returns:
        True when the environment opts the page in.
    """
    return os.getenv("PRELOOP_POLICIES_CONSOLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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

    # Registration state comes from the SAME computed rule /register
    # enforces (preloop.api.auth.bootstrap): an unclaimed instance (zero
    # users + bootstrap token configured) keeps the signup form reachable —
    # with the setup link — regardless of REGISTRATION_ENABLED.
    state = registration_state(db)
    result["features"]["registration"] = state.registration_open

    # First-account context for the signup form: when no user exists yet the
    # console explains that the account being created becomes the admin
    # account. Cached process-wide once the first user exists (never flips
    # back).
    result["features"]["first_account_pending"] = state.first_account_pending

    # True while the instance is unclaimed (zero users + bootstrap token
    # configured): the signup form must ask for the setup link.
    result["features"]["registration_bootstrap_pending"] = state.bootstrap_pending

    # Session optimization ships in the open-source core (0.12.0): the
    # capability is always present, so the console must always show it.
    # Deployments that meter hosted-model analysis gate at request time via
    # the optimization_gating authorizer (402), never by hiding the UI.
    # setdefault so a plugin that already set the flag keeps its value.
    result["features"].setdefault("session_optimization", True)

    # Policies console: off by default while the page is being reworked. The
    # backend policy APIs stay open; only the console page is gated. Instance
    # admins bypass the flag in the console shell.
    result["features"].setdefault("policies_console", policies_console_enabled())

    # Passkey (WebAuthn) support: PASSKEYS_ENABLED env, default true. The
    # login page uses this to decide whether to render "Sign in with passkey".
    # Import guarded so a missing/broken webauthn dependency degrades to
    # "passkeys: false" instead of breaking the whole features endpoint.
    try:
        from preloop.api.auth.webauthn_router import passkeys_enabled

        result["features"]["passkeys"] = passkeys_enabled()
    except Exception:
        result["features"]["passkeys"] = False

    return result
