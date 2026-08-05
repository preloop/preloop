"""Provider-native error envelopes for model gateway endpoints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

from preloop.utils.secret_scrubbing import scrub_secrets

GatewayProvider = Literal["openai", "anthropic", "gemini"]

# Hard cap on any upstream-provider text surfaced to end users. Upstream
# messages can embed multi-KB metadata blobs; the user needs the sentence,
# not the dump.
_MAX_SURFACED_MESSAGE_CHARS = 400
_MAX_PROVIDER_DETAIL_CHARS = 160

# litellm doubles (sometimes triples) its own exception-class prefix onto the
# provider message, e.g. "litellm.NotFoundError: litellm.NotFoundError: ...".
_LITELLM_PREFIX_RE = re.compile(r"^(?:litellm\.\w+(?:Error|Exception):\s*)+")
# "<Provider>Exception - " marker litellm inserts before the raw body, e.g.
# "OpenrouterException - {...}" or "OpenAIException - invalid request".
_PROVIDER_MARKER_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)Exception\s*-\s*")


@dataclass(frozen=True)
class UpstreamErrorDetail:
    """The upstream provider's own message, lifted out of a litellm blob.

    Attributes:
        message: The human-readable sentence the upstream provider produced,
            scrubbed of secrets and length-capped. Falls back to the cleaned
            litellm text when no structured body is present.
        provider_detail: A short hint (upstream provider name plus a compact
            context fragment), or ``None`` when nothing useful was found.
            Never a metadata dump.
    """

    message: str
    provider_detail: Optional[str] = None


def _parse_embedded_json(text: str) -> Optional[dict[str, Any]]:
    """Parse a JSON object embedded at (or after) the start of ``text``."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(text[start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _compact_hint(
    provider_name: Optional[str], body: Optional[dict[str, Any]]
) -> Optional[str]:
    """Build the short ``provider_detail`` hint. Never a metadata dump."""
    fragments: list[str] = []
    if provider_name:
        fragments.append(provider_name)
    if body is not None:
        error_obj = body.get("error")
        metadata = error_obj.get("metadata") if isinstance(error_obj, dict) else None
        if isinstance(metadata, dict):
            requested = metadata.get("requested_providers")
            if isinstance(requested, list) and requested:
                names = ", ".join(str(item) for item in requested[:3])
                fragments.append(f"(requested_providers: {names})")
    if not fragments:
        return None
    hint = " ".join(fragments)[:_MAX_PROVIDER_DETAIL_CHARS]
    return scrub_secrets(hint)


def extract_upstream_error_detail(raw_message: str) -> UpstreamErrorDetail:
    """Lift the upstream provider's own message out of a litellm error blob.

    Strips the repeated ``litellm.<Class>Error:`` prefixes and the
    ``<Provider>Exception - `` marker, then parses the embedded JSON body
    (``{"error": {"message": ...}}``) when present and lifts the inner
    human-readable message. Falls back to the cleaned litellm text when no
    parseable body exists. Everything surfaced is passed through
    :func:`scrub_secrets` and length-capped.

    Args:
        raw_message: The raw exception message, possibly litellm-wrapped.

    Returns:
        An :class:`UpstreamErrorDetail` with the user-facing message and an
        optional short provider hint.
    """
    text = str(raw_message).strip()
    text = _LITELLM_PREFIX_RE.sub("", text)

    provider_name: Optional[str] = None
    marker = _PROVIDER_MARKER_RE.match(text)
    if marker:
        provider_name = marker.group("name").lower()
        text = text[marker.end() :]

    body = _parse_embedded_json(text)
    message = text
    if body is not None:
        error_obj = body.get("error")
        inner: Any = None
        if isinstance(error_obj, dict):
            inner = error_obj.get("message")
        elif isinstance(error_obj, str):
            inner = error_obj
        elif isinstance(body.get("message"), str):
            inner = body.get("message")
        if isinstance(inner, str) and inner.strip():
            message = inner.strip()

    message = (scrub_secrets(message) or "Gateway upstream error")[
        :_MAX_SURFACED_MESSAGE_CHARS
    ]
    return UpstreamErrorDetail(
        message=message,
        provider_detail=_compact_hint(provider_name, body),
    )


def _default_error_type(provider: GatewayProvider, status_code: int) -> str:
    if provider == "gemini":
        if status_code == 400:
            return "INVALID_ARGUMENT"
        if status_code == 401:
            return "UNAUTHENTICATED"
        if status_code == 403:
            return "PERMISSION_DENIED"
        if status_code == 404:
            return "NOT_FOUND"
        if status_code == 429:
            return "RESOURCE_EXHAUSTED"
        if status_code >= 500:
            return "INTERNAL"
        return "UNKNOWN"

    if provider == "anthropic":
        if status_code == 400:
            return "invalid_request_error"
        if status_code == 401:
            return "authentication_error"
        if status_code == 403:
            return "permission_error"
        if status_code == 404:
            return "not_found_error"
        if status_code == 429:
            return "rate_limit_error"
        if status_code == 529:
            return "overloaded_error"
        return "api_error"

    if status_code == 400:
        return "invalid_request_error"
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    return "api_error"


@dataclass
class ModelGatewayAPIError(Exception):
    """Model gateway exception rendered in the target client format.

    ``error_class`` / ``retry_after_seconds`` / ``terminal`` carry the shared
    upstream-error classification (see ``preloop.services.upstream_errors``)
    so callers can record the failure category (#118) and the HTTP layer can
    emit ``Retry-After`` / terminal-retry hints (#114, #116). They do not
    change the provider-native response body shape.
    """

    provider: GatewayProvider
    status_code: int
    message: str
    error_type: Optional[str] = None
    param: Optional[str] = None
    code: Optional[str] = None
    error_class: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    terminal: bool = False
    provider_detail: Optional[str] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if not self.error_type:
            self.error_type = _default_error_type(self.provider, self.status_code)

    def to_payload(self) -> dict[str, Any]:
        """Return the provider-native error response body."""
        if self.provider == "gemini":
            return {
                "error": {
                    "code": self.status_code,
                    "message": self.message,
                    "status": self.error_type,
                }
            }

        if self.provider == "anthropic":
            return {
                "type": "error",
                "error": {
                    "type": self.error_type,
                    "message": self.message,
                },
            }

        error_payload: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "param": self.param,
            "code": self.code,
        }
        if self.provider_detail:
            error_payload["provider_detail"] = self.provider_detail
        return {"error": error_payload}

    def response_headers(self) -> dict[str, str]:
        """HTTP headers carrying retry/terminal hints for gateway clients."""
        headers: dict[str, str] = {}
        if self.retry_after_seconds is not None:
            headers["Retry-After"] = str(self.retry_after_seconds)
        if self.error_class:
            headers["X-Preloop-Error-Class"] = self.error_class
        if self.terminal:
            headers["X-Preloop-Retry-Terminal"] = "true"
        return headers
