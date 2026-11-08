"""GitHub repository_dispatch framing for restricted CI completion.

A repository_dispatch reaches a workflow as ``github.event.client_payload``
with no request headers, so the signature travels inside the payload. The
signed bytes travel too, as a JSON string, so the publisher verifies exactly
what was signed instead of a re-serialization of it:

``client_payload``::

    {
      "event": "<the v1 envelope, the exact bytes that were signed>",
      "signature": "t=<unix>,v1=<hex hmac-sha256 of t.event>",
      "event_id": "<envelope id>",
      "delivery_id": "<outbox row id>",
      "attempt": 1
    }

The signature uses the subscription signing secret and the same scheme as
``X-Preloop-Signature`` on HTTP deliveries.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from preloop.services.event_webhooks.signing import signature_header

# GitHub accepts at most 10 top-level client_payload properties and a bounded
# body; stay well under the documented 64 KB.
MAX_CLIENT_PAYLOAD_BYTES = 60_000
MAX_CLIENT_PAYLOAD_KEYS = 10
GITHUB_API_VERSION = "2022-11-28"


def client_payload(
    *,
    envelope: bytes,
    secret: str,
    event_id: str,
    delivery_id: str,
    attempt: int,
    timestamp: Optional[int] = None,
) -> dict[str, Any]:
    """Wrap one signed envelope for repository_dispatch.

    Args:
        envelope: The exact JSON bytes of the v1 envelope.
        secret: The subscription signing secret.
        event_id: Envelope id, for receiver deduplication.
        delivery_id: Outbox row id.
        attempt: 1-based attempt number.
        timestamp: Unix seconds; defaults to now.

    Returns:
        The ``client_payload`` object.
    """
    return {
        "event": envelope.decode("utf-8"),
        "signature": signature_header(secret, envelope, timestamp),
        "event_id": event_id,
        "delivery_id": delivery_id,
        "attempt": attempt,
    }


def request_body(event_type: str, payload: dict[str, Any]) -> bytes:
    """Encode the dispatch request body and enforce GitHub's payload limits.

    Raises:
        ValueError: When the payload breaks a GitHub limit or the event type
            is empty. The message names the limit, never the content.
    """
    if not event_type:
        raise ValueError("GitHub dispatch event_type is not configured")
    if len(payload) > MAX_CLIENT_PAYLOAD_KEYS:
        raise ValueError("GitHub dispatch client_payload has too many keys")
    encoded_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded_payload) > MAX_CLIENT_PAYLOAD_BYTES:
        raise ValueError("GitHub dispatch client_payload exceeds the size limit")
    return json.dumps(
        {"event_type": event_type, "client_payload": payload},
        separators=(",", ":"),
    ).encode("utf-8")


def request_headers(token: str, *, user_agent: str) -> dict[str, str]:
    """Headers for ``POST /repos/{owner}/{repo}/dispatches``."""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
