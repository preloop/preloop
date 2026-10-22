"""Optional network policy for registration and webhook connections.

Private collectors remain supported when ``webhook_block_private_targets`` is
false. When enabled, registration checks give immediate feedback and the delivery
transport resolves and pins checked addresses for each attempt. Response bodies
are not retained in delivery records.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

from preloop.config import settings


def _blocked_reason_for_address(raw: str) -> Optional[str]:
    """Return why one resolved address is off limits, or None."""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return "invalid address"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_private:
        return "private"
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return "reserved"
    if not address.is_global:
        return "non-global"
    return None


def checked_target_addresses(host: str) -> list[str]:
    """Resolve once and reject the entire answer if any address is disallowed."""
    if not host:
        raise ValueError("no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = list(
                dict.fromkeys(
                    str(info[4][0]) for info in socket.getaddrinfo(host, None)
                )
            )
        except socket.gaierror as exc:
            raise ValueError("unresolvable") from exc
    else:
        addresses = [str(literal)]
    if not addresses:
        raise ValueError("unresolvable")
    for address in addresses:
        reason = _blocked_reason_for_address(address)
        if reason:
            raise ValueError(reason)
    return addresses


def blocked_target_reason(url: str) -> Optional[str]:
    """Return why this URL may not be registered, or None if it is allowed.

    Args:
        url: The absolute http(s) URL an operator wants to register.

    Returns:
        A short reason string ("loopback", "private", "unresolvable", ...) or
        None. Always None while ``webhook_block_private_targets`` is off.
    """
    if not settings.webhook_block_private_targets:
        return None
    try:
        checked_target_addresses((urlparse(url).hostname or "").strip())
    except ValueError as exc:
        return str(exc)
    return None
