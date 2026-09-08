"""Optional guard against pointing a webhook at internal address space.

A webhook URL is operator-supplied and the delivery worker runs inside the
cluster, so an unchecked URL is a server-side request forgery primitive: an
account admin could aim an endpoint at a metadata service or a neighbouring
pod and read the first bytes of the response back through the delivery log.

Off by default (``webhook_block_private_targets``), because a self-hosted
deployment posting to a collector on the same private network is the normal
case, not the attack. Turn it on for multi-tenant hosting.

The check runs when the endpoint is registered, not on every attempt: it
resolves DNS once, so a name that later re-resolves to a private address is
not caught here. It raises the bar; it is not a sandbox.
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
        return None
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_private:
        return "private"
    if address.is_reserved or address.is_multicast or address.is_unspecified:
        return "reserved"
    return None


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
    host = (urlparse(url).hostname or "").strip()
    if not host:
        return "no host"
    direct = _blocked_reason_for_address(host)
    if direct:
        return direct
    try:
        resolved = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # A name that does not resolve now cannot be checked, and delivering
        # to it would fail anyway. Refuse rather than register blind.
        return "unresolvable"
    for info in resolved:
        reason = _blocked_reason_for_address(str(info[4][0]))
        if reason:
            return reason
    return None
