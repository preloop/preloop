"""Protect execution credentials in durable control command storage."""

from copy import deepcopy
from typing import Any

from preloop.utils.encryption import decrypt_value, encrypt_value


def protect_control_credentials(envelope: dict[str, Any]) -> dict[str, Any]:
    """Encrypt a gateway credential without mutating the delivery envelope."""
    stored = deepcopy(envelope)
    gateway = stored.get("payload", {}).get("metadata", {}).get("gateway")
    if isinstance(gateway, dict) and gateway.get("api_key"):
        gateway["encrypted_api_key"] = encrypt_value(gateway.pop("api_key"))
    return stored


def hydrate_control_credentials(envelope: dict[str, Any]) -> dict[str, Any]:
    """Resolve encrypted credentials only for authorized runtime delivery."""
    delivery = deepcopy(envelope)
    gateway = delivery.get("payload", {}).get("metadata", {}).get("gateway")
    if isinstance(gateway, dict) and gateway.get("encrypted_api_key"):
        gateway["api_key"] = decrypt_value(gateway.pop("encrypted_api_key"))
    return delivery
