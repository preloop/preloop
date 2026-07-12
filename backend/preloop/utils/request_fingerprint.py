"""Truncate internal request fingerprints before exposing them on APIs."""

from __future__ import annotations

from typing import Optional

# Full SHA-256 fingerprints stay in DB/meta for retry grouping; API responses
# only need enough bits to correlate attempts without publishing the full hash.
_PUBLIC_FINGERPRINT_CHARS = 16


def public_request_fingerprint(value: Optional[str]) -> Optional[str]:
    """Return a truncated fingerprint safe for account-facing API payloads."""
    if not isinstance(value, str) or not value:
        return None
    return value[:_PUBLIC_FINGERPRINT_CHARS]
