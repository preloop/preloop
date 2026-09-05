"""TLS verify settings for provider HTTP clients (private PKI).

httpx, the OpenAI SDK, and LiteLLM default to certifi and do not honor
``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` on their own. Operators mount a
private CA into the gateway/API pods and set those env vars; this helper
turns them into the ``verify`` / ``ssl_verify`` value those clients accept.
"""

from __future__ import annotations

import os
from typing import Optional, Union

SslVerify = Union[bool, str]

_SKIP_VERIFY_VALUES = frozenset({"0", "false", "no", "off"})
_CA_BUNDLE_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def ssl_verify_setting() -> Optional[SslVerify]:
    """Return a TLS verify override for provider HTTP clients, if any.

    Resolution order:

    1. ``PRELOOP_SSL_VERIFY`` set to a false-like value (``false``, ``0``,
       ``no``, ``off``): return ``False`` so verification is skipped. This is
       a last-resort escape hatch for private PKI, not the default.
    2. ``SSL_CERT_FILE``, then ``REQUESTS_CA_BUNDLE``, then ``CURL_CA_BUNDLE``:
       return the first non-empty path so httpx/LiteLLM trust that bundle.
    3. Otherwise ``None`` so callers leave the client default (certifi /
       system store) unchanged.

    Returns:
        ``False`` to skip verify, a filesystem path to a CA bundle, or
        ``None`` when no override is configured.
    """
    raw = (os.environ.get("PRELOOP_SSL_VERIFY") or "").strip().lower()
    if raw in _SKIP_VERIFY_VALUES:
        return False
    for key in _CA_BUNDLE_ENV_VARS:
        path = (os.environ.get(key) or "").strip()
        if path:
            return path
    return None
