"""Optional billing policy hook, retaining the environment fallback for core."""

import logging
from typing import Any

from preloop.config import settings
from preloop.plugins import get_plugin_manager


logger = logging.getLogger(__name__)


def entitlements_enforced(account_id: Any) -> bool:
    """Ask billing for the account decision when installed; env off is absolute."""
    if not settings.billing_enforce_entitlements:
        return False
    resolver = get_plugin_manager().get_service("entitlement_enforcement")
    if not callable(resolver):
        return True
    try:
        return bool(resolver(account_id))
    except Exception:
        # Retain the pre-plugin policy when an optional extension fails. Do
        # not log exception text, which could contain connection details.
        logger.warning("Entitlement policy unavailable; using environment fallback")
        return bool(settings.billing_enforce_entitlements)
