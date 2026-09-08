"""HMAC-SHA256 signing for outbound event webhooks.

The signature covers ``f"{timestamp}.{body}"`` with the endpoint secret. It
proves that whoever produced the request held that secret at that timestamp.
It says nothing about anything Preloop stores; see docs/guide/webhooks.md for
the explicit list of what is not signed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

SECRET_PREFIX = "whsec_"
SIGNATURE_HEADER = "X-Preloop-Signature"
EVENT_ID_HEADER = "X-Preloop-Event-Id"
EVENT_TYPE_HEADER = "X-Preloop-Event-Type"
DELIVERY_ID_HEADER = "X-Preloop-Delivery-Id"
ATTEMPT_HEADER = "X-Preloop-Attempt"
USER_AGENT = "Preloop-Webhook/1"
# Verification tolerance recommended to receivers, in seconds. Preloop stamps a
# fresh timestamp on every attempt, so a delivery retried an hour later still
# lands inside this window.
DEFAULT_TOLERANCE_SECONDS = 300


def generate_secret() -> str:
    """Return a new endpoint secret. Shown to the operator exactly once."""
    return f"{SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def secret_hint(secret: str) -> str:
    """Return the trailing characters kept for identifying a secret."""
    return secret[-4:] if len(secret) >= 4 else ""


def signed_payload(timestamp: int, body: bytes) -> bytes:
    """Return the exact bytes the HMAC is computed over."""
    return str(int(timestamp)).encode("ascii") + b"." + body


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    """Return the hex HMAC-SHA256 of ``timestamp.body`` under ``secret``."""
    return hmac.new(
        secret.encode("utf-8"),
        signed_payload(timestamp, body),
        hashlib.sha256,
    ).hexdigest()


def signature_header(secret: str, body: bytes, timestamp: Optional[int] = None) -> str:
    """Build the ``t=...,v1=...`` header value for one attempt."""
    stamp = int(time.time()) if timestamp is None else int(timestamp)
    return f"t={stamp},v1={compute_signature(secret, stamp, body)}"


def parse_signature_header(value: str) -> tuple[Optional[int], list[str]]:
    """Split a signature header into its timestamp and its v1 signatures.

    Unknown parts are ignored so a future ``v2=`` can be added without
    breaking a receiver written against v1.

    Args:
        value: Raw header value.

    Returns:
        The parsed timestamp (or None when absent/unparsable) and every
        ``v1`` signature found.
    """
    timestamp: Optional[int] = None
    signatures: list[str] = []
    for part in (value or "").split(","):
        key, _, raw = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(raw)
            except ValueError:
                timestamp = None
        elif key == "v1" and raw:
            signatures.append(raw)
    return timestamp, signatures


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> bool:
    """Verify a signature header the way a receiver should.

    Shipped in the product so the documented verification sample is the code
    the tests exercise, not prose that drifted.

    Args:
        secret: The endpoint secret.
        header: Raw ``X-Preloop-Signature`` value.
        body: The raw request body bytes, before any JSON reparse.
        tolerance_seconds: Maximum accepted clock skew, each direction.
        now: Current unix time; defaults to the wall clock.

    Returns:
        True when the timestamp is inside tolerance and a v1 signature
        matches.
    """
    timestamp, signatures = parse_signature_header(header)
    if timestamp is None or not signatures:
        return False
    current = int(time.time()) if now is None else int(now)
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = compute_signature(secret, timestamp, body)
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)
