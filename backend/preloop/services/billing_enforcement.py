"""Optional billing policy hook, retaining the environment fallback for core."""

from typing import Any

from preloop.config import settings
from preloop.plugins import get_plugin_manager


def entitlements_enforced(account_id: Any) -> bool:
    """Ask billing for the account decision when installed; env off is absolute."""
    if not settings.billing_enforce_entitlements:
        return False
    resolver = get_plugin_manager().get_service("entitlement_enforcement")
    return bool(resolver(account_id)) if callable(resolver) else True
