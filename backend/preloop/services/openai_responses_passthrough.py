"""Routing rules for the native OpenAI Responses passthrough (issue #159).

Why this exists
---------------

``POST /openai/v1/responses`` used to have exactly one non-Codex
implementation: normalize the Responses payload into chat messages and hand
them to LiteLLM, which then calls the upstream's **chat completions**
endpoint. A request that entered Preloop through the Responses API therefore
left Preloop through a different API. That transcode is lossy and it fails in
two ways that were both reported on issue #159:

1. Codex CLI's Responses payloads carry tool shapes (``namespace``
   containers, freeform ``custom`` tools) that have no chat-completions
   representation. :mod:`preloop.services.codex_tool_compat` flattens them,
   but flattening is a workaround for a translation that should not have to
   happen.
2. An upstream can implement the Responses API and *not* implement chat
   completions for the same model. Then the transcode cannot work at all.
   Measured case: OpenCode Zen (``https://opencode.ai/zen/v1``) with
   ``muse-spark-1.3-contributor-free`` answers ``POST /responses`` with 200
   and ``POST /chat/completions`` with 500, so every Responses request
   through Preloop returned 502 ``InternalServerError: OpenAIException -
   Internal server error``.

The fix is to stop translating: when the upstream is OpenAI-shaped and
authenticates with an API key, forward the Responses payload to the
upstream's own ``/responses`` endpoint and relay the answer. Preloop keeps
doing everything that makes it a gateway (auth, model authorization, policy,
governance tool-stripping, budget checks, usage accounting, audit); only the
lossy protocol translation goes away.

Fallback, not a flag day
------------------------

Plenty of OpenAI-compatible upstreams implement chat completions only. They
must keep working, and their owners should not have to configure anything.
So the default mode is ``auto``: try the native Responses endpoint, and if
the upstream answers "no such endpoint" (404 / 405 / 501), remember that for
this base URL and fall back to the existing transcode. The probe costs one
round trip against a non-existent path, once per upstream per TTL, and
never any tokens.

Operators who want to skip the probe (or force the old behaviour) can set
``meta_data.gateway.responses_api`` on the model row to ``native`` or
``transcode``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from preloop.services.context_optimization import tool_choice_named_tool
from preloop.services.litellm_routing import is_openrouter_model, to_litellm_model

# Responses ingress modes, stored on ``AIModel.meta_data.gateway.responses_api``.
PASSTHROUGH_MODE_AUTO = "auto"
PASSTHROUGH_MODE_NATIVE = "native"
PASSTHROUGH_MODE_TRANSCODE = "transcode"
PASSTHROUGH_MODES = frozenset(
    {PASSTHROUGH_MODE_AUTO, PASSTHROUGH_MODE_NATIVE, PASSTHROUGH_MODE_TRANSCODE}
)

# Where an OpenAI BYOK model with no explicit endpoint sends Responses traffic.
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

# Status codes that mean "this upstream has no Responses endpoint", as opposed
# to "the Responses endpoint rejected this request". Only these trigger the
# fallback to the chat-completions transcode; a 400/401/422 is a real error
# and is surfaced, because retrying it through a different protocol would
# hide a genuine problem behind a confusing second failure.
RESPONSES_API_ABSENT_STATUS_CODES = frozenset({404, 405, 501})

# How long a negative capability probe is trusted. Long enough that a busy
# gateway probes once, short enough that an upstream which ships Responses
# support is picked up the same day without a restart.
CAPABILITY_CACHE_TTL_SECONDS = 900.0

_CAPABILITY_CACHE: Dict[str, float] = {}
_CAPABILITY_CACHE_LOCK = threading.Lock()


def responses_passthrough_mode(ai_model: Any) -> str:
    """Return the configured Responses ingress mode for a model row.

    Reads ``meta_data.gateway.responses_api`` and falls back to the flat
    ``meta_data.responses_api``. Unknown values are treated as ``auto`` so a
    typo degrades to the safe self-detecting behaviour instead of pinning
    traffic to one protocol.

    Args:
        ai_model: The resolved model row (or any object with ``meta_data``).

    Returns:
        One of ``auto``, ``native``, ``transcode``.
    """
    meta_data = getattr(ai_model, "meta_data", None)
    if not isinstance(meta_data, dict):
        return PASSTHROUGH_MODE_AUTO
    gateway_meta = meta_data.get("gateway")
    raw = None
    if isinstance(gateway_meta, dict):
        raw = gateway_meta.get("responses_api")
    if raw is None:
        raw = meta_data.get("responses_api")
    if isinstance(raw, str) and raw.strip().lower() in PASSTHROUGH_MODES:
        return raw.strip().lower()
    return PASSTHROUGH_MODE_AUTO


def is_openai_shaped_upstream(ai_model: Any) -> bool:
    """Whether this model's traffic terminates at an OpenAI-shaped HTTP API.

    The test is deliberately the same one LiteLLM routing already makes: a
    model whose LiteLLM string is ``openai/<id>`` is exactly a model LiteLLM
    would drive through its generic OpenAI adapter against ``api_base``. That
    covers provider ``openai`` (BYOK, api.openai.com), ``openai-compatible``,
    ``custom``, and any unknown provider name that carries its own endpoint.
    It excludes Anthropic, Gemini, Bedrock, and anything whose identifier
    pins a different LiteLLM provider prefix.

    OpenRouter is excluded on purpose: its LiteLLM adapter carries the usage
    accounting and app-attribution behaviour the cost pipeline depends on
    (issue #219), so its traffic stays on that adapter.

    Args:
        ai_model: The resolved model row.

    Returns:
        True when a native Responses passthrough is structurally possible.
    """
    if is_openrouter_model(ai_model):
        return False
    try:
        litellm_model = to_litellm_model(ai_model)
    except Exception:
        return False
    return litellm_model.startswith("openai/")


def responses_passthrough_base_url(ai_model: Any) -> str:
    """Return the base URL the native Responses request should target."""
    endpoint = getattr(ai_model, "api_endpoint", None)
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip().rstrip("/")
    return DEFAULT_OPENAI_BASE_URL


def responses_passthrough_url(ai_model: Any) -> str:
    """Return the full ``/responses`` URL for a model row.

    An endpoint already pointing at ``/responses`` is honoured as-is so an
    operator who configured the exact URL is not given ``/responses/responses``.
    """
    base = responses_passthrough_base_url(ai_model)
    if base.endswith("/responses"):
        return base
    return f"{base}/responses"


def capability_cache_key(ai_model: Any) -> str:
    """Key under which "no Responses endpoint here" is remembered.

    Keyed on the base URL only, not the model id: the Responses endpoint is a
    property of the deployment, and one probe per upstream is the point.
    """
    return responses_passthrough_base_url(ai_model).lower()


def mark_responses_api_absent(cache_key: str, *, now: Optional[float] = None) -> None:
    """Remember that an upstream answered "no such endpoint" for Responses."""
    expiry = (
        now if now is not None else time.monotonic()
    ) + CAPABILITY_CACHE_TTL_SECONDS
    with _CAPABILITY_CACHE_LOCK:
        _CAPABILITY_CACHE[cache_key] = expiry


def is_responses_api_absent(cache_key: str, *, now: Optional[float] = None) -> bool:
    """Whether a recent probe found no Responses endpoint at this upstream."""
    current = now if now is not None else time.monotonic()
    with _CAPABILITY_CACHE_LOCK:
        expiry = _CAPABILITY_CACHE.get(cache_key)
        if expiry is None:
            return False
        if expiry <= current:
            _CAPABILITY_CACHE.pop(cache_key, None)
            return False
        return True


def reset_capability_cache() -> None:
    """Forget every capability probe (tests, and admin-initiated re-probe)."""
    with _CAPABILITY_CACHE_LOCK:
        _CAPABILITY_CACHE.clear()


def should_use_responses_passthrough(ai_model: Any) -> bool:
    """Whether this request should go out as a native Responses call.

    Args:
        ai_model: The resolved model row.

    Returns:
        True to forward natively, False to use the chat-completions transcode.
    """
    mode = responses_passthrough_mode(ai_model)
    if mode == PASSTHROUGH_MODE_TRANSCODE:
        return False
    if not is_openai_shaped_upstream(ai_model):
        return False
    if mode == PASSTHROUGH_MODE_NATIVE:
        return True
    return not is_responses_api_absent(capability_cache_key(ai_model))


def build_passthrough_body(
    ai_model: Any, payload: Dict[str, Any], *, stream: bool
) -> Dict[str, Any]:
    """Build the outbound Responses body from the client's payload.

    The payload is forwarded as received. Only two things change, and both
    are gateway responsibilities rather than protocol translation:

    * ``model`` becomes the upstream model identifier, because the client
      addressed a Preloop gateway alias;
    * ``stream`` is set to match the ingress the client actually used, so a
      non-streaming ingress cannot be answered with an SSE body.

    Everything else, including ``instructions``, ``reasoning``, ``include``,
    ``store``, ``prompt_cache_key``, ``text``, ``truncation`` and the full
    ``input`` history with its tool-call items, survives untouched. Those are
    exactly the fields the chat-completions transcode used to drop.

    Args:
        ai_model: The resolved model row.
        payload: The client's Responses request payload (already governed).
        stream: Whether the client asked for a streaming response.

    Returns:
        A new dict; ``payload`` is not mutated.
    """
    body = dict(payload)
    body["model"] = getattr(ai_model, "model_identifier", None) or body.get("model")
    if stream:
        body["stream"] = True
    else:
        body.pop("stream", None)
    return body


def responses_tool_choice_named_tool(tool_choice: Any) -> Optional[str]:
    """Extract the tool name a Responses-shaped ``tool_choice`` forces.

    The Responses API names the tool at the top level
    (``{"type": "function", "name": "shell"}``), where chat completions nests
    it (``{"type": "function", "function": {"name": "shell"}}``). The shared
    :func:`tool_choice_named_tool` knows the chat and Anthropic shapes only,
    so on the passthrough it would return ``None`` for a real Codex request
    and a ``tool_choice`` pointing at a governance-stripped tool would reach
    the upstream and 400 the whole turn.

    Args:
        tool_choice: The request ``tool_choice`` value (any shape).

    Returns:
        The forced tool name, or ``None`` when the choice names no tool.
    """
    shared = tool_choice_named_tool(tool_choice)
    if shared:
        return shared
    if not isinstance(tool_choice, dict):
        return None
    name = tool_choice.get("name")
    if isinstance(name, str) and name:
        return name
    return None


def passthrough_host(ai_model: Any) -> str:
    """Host of the passthrough upstream, for log lines that must not leak paths."""
    try:
        return urlsplit(responses_passthrough_base_url(ai_model)).netloc or "unknown"
    except ValueError:
        return "unknown"
