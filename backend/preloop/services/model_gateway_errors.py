"""Provider-native error envelopes for model gateway endpoints."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

from preloop.utils.secret_scrubbing import scrub_secrets

GatewayProvider = Literal["openai", "anthropic", "gemini"]
ErrorOrigin = Literal["upstream", "gateway"]

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
        provider_name: The lowercased provider name from litellm's
            ``<Provider>Exception`` marker, or ``None`` when absent.
    """

    message: str
    provider_detail: Optional[str] = None
    provider_name: Optional[str] = None


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
        provider_name=provider_name,
    )


#: Provider-reported statuses whose sentence used to reach the client bare,
#: on Preloop's URL, with nothing naming the upstream as its source (#2288).
ORIGIN_LABELLED_UPSTREAM_STATUSES = frozenset({401, 402, 403, 404})


def _as_sentence(text: str) -> str:
    """Return ``text`` ending in sentence punctuation."""
    stripped = text.strip()
    if not stripped or stripped[-1] in ".!?":
        return stripped
    return f"{stripped}."


def upstream_origin_message(
    provider_name: Optional[str], status_code: int, provider_message: str
) -> str:
    """Say that a provider, not Preloop, produced this client-visible error.

    Only call this once an upstream call has been attempted: by then Preloop
    has accepted the client's own credentials. The provider's sentence is
    kept verbatim (already scrubbed and capped by the caller) and labelled
    as the provider's; it is never interpreted.

    Args:
        provider_name: The configured upstream provider (for example
            ``openrouter``), or ``None`` when unknown.
        status_code: The HTTP status the provider reported.
        provider_message: The provider's own scrubbed sentence.

    Returns:
        The labelled client-facing message.
    """
    who = (
        f"Upstream provider {provider_name}"
        if provider_name
        else ("The upstream provider")
    )
    quoted = _as_sentence(provider_message) or "(none)"
    if status_code == 401:
        return (
            f"{who} rejected the provider credential configured for this model "
            f"(HTTP {status_code}). Provider message: {quoted} This is not a "
            "rejection of your Preloop credentials."
        )
    return (
        f"{who} returned HTTP {status_code} for this request. Provider "
        f"message: {quoted} This error came from the upstream provider, not "
        "from Preloop."
    )


def gemini_origin_details(
    fields: dict[str, Any], *, fallback_reason: Optional[str] = None
) -> list[dict[str, Any]]:
    """Render origin fields (#2288) as Gemini ``google.rpc`` error details.

    Google's error envelope carries extras in ``details``; ``ErrorInfo``
    metadata is a string map, so values become strings and the nested
    ``cause`` becomes JSON.

    Args:
        fields: Output of :meth:`ModelGatewayAPIError.origin_fields`.
        fallback_reason: Reason used when ``fields`` has no ``category``.

    Returns:
        A one-element ``details`` list holding the ``ErrorInfo``.
    """
    reason = str(fields.get("category") or fallback_reason or "gateway_error")
    return [
        {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": reason.upper(),
            "domain": "preloop.ai",
            "metadata": {
                key: (
                    json.dumps(value, sort_keys=True)
                    if isinstance(value, dict)
                    else str(value)
                )
                for key, value in fields.items()
            },
        }
    ]


_ORIGIN_LABEL_RE = re.compile(
    r"^(?:Upstream provider \S+|The upstream provider) (?:rejected the provider "
    r"credential configured for this model \(HTTP \d{3}\)|returned HTTP \d{3} "
    r"for this request)\. Provider message: (?P<message>.*?) (?:This is not a "
    r"rejection of your Preloop credentials|This error came from the upstream "
    r"provider, not from Preloop)\.$",
    re.DOTALL,
)


def provider_message_from_label(text: Optional[str]) -> Optional[str]:
    """Return the provider's own sentence from an origin-labelled message.

    Inverse of :func:`upstream_origin_message`, for consumers that store the
    provider's wording on its own (the model's credential health). Text
    without the label is returned unchanged.

    Args:
        text: A recorded gateway error message, possibly labelled.

    Returns:
        The provider's sentence, or ``text`` when it carries no label.
    """
    if not text:
        return text
    match = _ORIGIN_LABEL_RE.match(text.strip())
    return match.group("message") if match else text


_MAX_ALERT_UPSTREAM_BODY_CHARS = 300


def summarize_upstream_body_for_alert(
    raw_body: Any, *, limit: int = _MAX_ALERT_UPSTREAM_BODY_CHARS
) -> str:
    """Render an upstream error body as one short line for operator alerts.

    Upstream error bodies are relayed verbatim and can be multi-line foreign
    stack traces (for example a customer-run LiteLLM proxy echoing its own
    Python traceback). Printed whole in an alert they read like a Preloop
    crash. Alerts therefore show only the first non-empty line, capped at
    ``limit`` characters, with an explicit truncation note. The full body is
    still recorded on the usage/audit row.

    Args:
        raw_body: The upstream text (any object; ``str()`` is applied).
        limit: Maximum characters kept from the first line.

    Returns:
        The scrubbed one-line summary, suffixed with
        ``(upstream body truncated, N chars)`` when anything was dropped.
    """
    full = (scrub_secrets(str(raw_body if raw_body is not None else "")) or "").strip()
    if not full:
        return "(empty)"
    first_line = next((line.strip() for line in full.splitlines() if line.strip()), "")
    summary = first_line[:limit]
    if summary != full:
        summary = f"{summary} (upstream body truncated, {len(full)} chars)"
    return summary


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
    # Where the failure was decided (#2288): ``upstream`` when the model
    # provider produced it, ``gateway`` when Preloop did. ``None`` keeps the
    # body exactly as it was for errors that do not set it.
    origin: Optional[ErrorOrigin] = None
    upstream_provider: Optional[str] = None
    upstream_status: Optional[int] = None
    request_id: Optional[str] = None
    # The first failure of a run that the gateway stopped, rendered as a
    # nested ``cause`` object (see ``origin_fields``).
    cause: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if not self.error_type:
            self.error_type = _default_error_type(self.provider, self.status_code)

    def origin_fields(self) -> dict[str, Any]:
        """Machine-readable origin fields for the error object (#2288).

        Additive keys placed next to ``message`` inside each protocol's error
        object, where the OpenAI, Anthropic and Gemini SDKs ignore unknown
        fields. Empty when ``origin`` is unset, so bodies of errors that do
        not opt in are unchanged.

        Returns:
            ``origin`` plus whichever of ``provider``, ``category``,
            ``upstream_status``, ``request_id`` and ``cause`` are known.
        """
        if self.origin is None:
            return {}
        fields: dict[str, Any] = {"origin": self.origin}
        if self.upstream_provider:
            fields["provider"] = self.upstream_provider
        if self.error_class:
            fields["category"] = self.error_class
        if self.upstream_status is not None:
            fields["upstream_status"] = self.upstream_status
        if self.request_id:
            fields["request_id"] = self.request_id
        if self.cause:
            fields["cause"] = dict(self.cause)
        return fields

    def to_payload(self) -> dict[str, Any]:
        """Return the provider-native error response body."""
        if self.provider == "gemini":
            gemini_error: dict[str, Any] = {
                "code": self.status_code,
                "message": self.message,
                "status": self.error_type,
            }
            origin = self.origin_fields()
            if origin:
                # Google's envelope carries extras as google.rpc details;
                # ErrorInfo metadata is a string map, so values are strings
                # and the nested cause is JSON.
                gemini_error["details"] = gemini_origin_details(
                    origin, fallback_reason=self.code or self.error_type
                )
            return {"error": gemini_error}

        if self.provider == "anthropic":
            return {
                "type": "error",
                "error": {
                    "type": self.error_type,
                    "message": self.message,
                    **self.origin_fields(),
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
        error_payload.update(self.origin_fields())
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
        headers.update(getattr(self, "extra_response_headers", None) or {})
        return headers
