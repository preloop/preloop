"""
Auxiliary model credential resolution and system-default fallback orchestration.

This module provides utilities for resolving AI model credentials and implementing
call-time fallback to the system-wide default model for non-interactive, auxiliary
generation paths (approval summaries, session titles, policy generation, etc.).

The main gateway/completion path must NOT use these fallback utilities.

Note on daily cap: the fallback cap is enforced per-process only. In a multi-process
deployment (gunicorn with multiple workers), each process tracks its own counter.
This is acceptable for aux paths; over-the-cap failures degrade gracefully.
"""

import asyncio
import logging
import os
from collections import defaultdict
from datetime import date, datetime, UTC
from threading import Lock
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from preloop.models.crud.ai_model import ai_model as crud_ai_model
from preloop.models.models.ai_model import AIModel
from preloop.services.aux_model_retry import call_with_aux_retry
from preloop.services.litellm_routing import apply_preloop_client_headers
from preloop.services.secret_service import get_secret_service

logger = logging.getLogger(__name__)

# Default daily cap for fallback generations per account, per UTC day.
DEFAULT_AUX_FALLBACK_DAILY_CAP = 50

# In-process fallback counters: {(account_id, utc_date): count}
_fallback_counters: dict[tuple[str, date], int] = defaultdict(int)
_fallback_counters_lock = Lock()

# In-process per-account warning dedupe: {(account_id, utc_date): logged}
_fallback_warnings: dict[tuple[str, date], bool] = defaultdict(bool)
_fallback_warnings_lock = Lock()


def get_aux_fallback_daily_cap() -> int:
    """Read the per-account daily fallback cap.

    Read at call time (not import time) so deployments and tests can change
    PRELOOP_AUX_FALLBACK_DAILY_CAP without re-importing this module. Invalid
    or negative values fall back to the default.
    """
    raw = os.getenv("PRELOOP_AUX_FALLBACK_DAILY_CAP")
    if raw is None:
        return DEFAULT_AUX_FALLBACK_DAILY_CAP
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid PRELOOP_AUX_FALLBACK_DAILY_CAP=%r; using default %d.",
            raw,
            DEFAULT_AUX_FALLBACK_DAILY_CAP,
        )
        return DEFAULT_AUX_FALLBACK_DAILY_CAP
    if value < 0:
        return DEFAULT_AUX_FALLBACK_DAILY_CAP
    return value


def _prune_stale_days(store: dict[tuple[str, date], Any], today: date) -> None:
    """Drop entries from previous days so the in-process maps stay bounded.

    Callers must already hold the lock guarding `store`.
    """
    stale = [key for key in store if key[1] != today]
    for key in stale:
        del store[key]


def resolve_model_call_credentials(
    model: AIModel, *, db: Optional[Session] = None
) -> dict[str, Any]:
    """
    Resolve AI model credentials and return litellm/openai kwargs fragment.

    This helper resolves credentials via the secret service, falling back to the
    legacy plaintext api_key column if needed. It handles different credential types
    (API key, OAuth, ambient) and only injects an api_key when credential_type == "api_key".

    Args:
        model: The AI model whose credentials to resolve.
        db: Optional database session (unused currently, for future compatibility).

    Returns:
        A dict of litellm/openai-compatible kwargs (e.g., {"api_key": "...", "api_base": "..."}).
        Never raises: on resolution failure the api_key is omitted so the caller
        degrades the same way it did before, but routing (api_base) is preserved.
    """
    kwargs: dict[str, Any] = {}

    # Routing is independent of credentials. Set it outside the try so a secret
    # backend failure cannot silently send the request to the wrong endpoint.
    if model.api_endpoint:
        kwargs["api_base"] = model.api_endpoint

    try:
        resolved = get_secret_service().resolve_ai_model_credentials(
            model, db=db, allow_refresh=False
        )
        if resolved:
            # Only inject api_key when credential_type is "api_key".
            # OAuth/ambient credentials do not provide an explicit key.
            if resolved.credential_type == "api_key" and resolved.value:
                kwargs["api_key"] = resolved.value

    except Exception as exc:
        logger.warning(
            "Failed to resolve credentials for model %s (%s): %s. Proceeding without an api_key.",
            model.id,
            model.model_identifier,
            exc,
            exc_info=True,
        )

    return kwargs


# Keys from model_parameters that are safe to pass to OpenAI SDK
# chat.completions.create() as named arguments. Kept narrow: unknown keys
# could collide with SDK-internal parameters or future additions.
_OPENAI_SDK_SAFE_MODEL_PARAM_KEYS = frozenset(
    {
        "reasoning_effort",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "seed",
    }
)


class ReasoningModelEmptyContentError(Exception):
    """A reasoning model exhausted its token budget on reasoning and returned empty content.

    Raised by :func:`check_reasoning_model_empty_content` so that
    :func:`call_with_default_model_fallback` can retry on the system default model.
    """


def _model_supports_none_reasoning_effort(
    model_identifier: str, custom_llm_provider: str
) -> bool:
    """Check whether litellm's model map says the model accepts reasoning_effort="none".

    Returns False on any error or when the model is not in litellm's map.
    """
    try:
        from litellm.utils import _supports_factory

        return bool(
            _supports_factory(
                model=model_identifier,
                custom_llm_provider=custom_llm_provider,
                key="supports_none_reasoning_effort",
            )
        )
    except Exception:
        return False


def _get_reasoning_disable_defaults(model: AIModel) -> dict[str, Any]:
    """Return capability-checked kwargs that disable extended reasoning for aux calls.

    Only returns a disable knob when there is positive evidence the model
    accepts that exact form. Unknown models, or any lookup failure, produce
    an empty dict so the model behaves as it does by default. The
    empty-content guard (:func:`check_reasoning_model_empty_content`) catches
    the bad outcome if reasoning runs away.

    OpenRouter: routing wins over the vendor prefix in the model id, exactly
    as in :func:`preloop.services.litellm_routing.to_litellm_model`. A model
    like ``deepseek/deepseek-v4-flash-0731`` on ``openrouter.ai`` speaks
    OpenRouter's API, not DeepSeek's, so the DeepSeek-native ``thinking`` knob
    is silently ignored there and reasoning runs anyway (2026-08-06 prod:
    every aux call on such a config burned ~600 reasoning tokens and timed
    out). OpenRouter's own knob is ``extra_body={"reasoning": {"enabled":
    false}}``, which litellm's OpenrouterConfig passes through verbatim.

    DeepSeek: ``extra_body={"thinking": {"type": "disabled"}}`` is the only
    form that survives ``DeepSeekChatConfig.map_openai_params``; the
    ``reasoning_effort`` kwarg and ``thinking={"type":"disabled"}`` as a
    top-level kwarg are both silently swallowed.

    OpenAI: ``reasoning_effort="none"`` is only valid for models whose litellm
    model map entry has ``supports_none_reasoning_effort: True`` (e.g. gpt-5.1).
    Models like o4-mini support ``reasoning_effort`` but NOT the ``"none"``
    level, so sending it would produce a 400.
    """
    from preloop.services.litellm_routing import is_openrouter_endpoint

    provider = (getattr(model, "provider_name", None) or "").strip().lower()
    identifier = (getattr(model, "model_identifier", None) or "").strip()
    endpoint = getattr(model, "api_endpoint", None)

    # OpenRouter routing outranks the vendor prefix inside the model id: the
    # upstream speaks OpenRouter's parameter surface, not the vendor's.
    if provider == "openrouter" or is_openrouter_endpoint(endpoint):
        return {"extra_body": {"reasoning": {"enabled": False}}}

    # DeepSeek: the only wire-surviving form is extra_body.
    if provider == "deepseek" or (
        "/" in identifier and identifier.split("/", 1)[0].strip().lower() == "deepseek"
    ):
        return {"extra_body": {"thinking": {"type": "disabled"}}}

    # OpenAI (and openai-codex, or empty-provider which defaults to openai):
    # only send reasoning_effort="none" when litellm confirms the model
    # accepts that level.
    if provider in ("openai", "openai-codex", ""):
        lookup = identifier.split("/", 1)[-1] if "/" in identifier else identifier
        if _model_supports_none_reasoning_effort(lookup, "openai"):
            return {"reasoning_effort": "none"}

    return {}


def _merge_extra_body(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge *source* into *target*, deep-merging ``extra_body`` dicts.

    All keys are overwritten by *source* except ``extra_body``: when both
    sides carry a dict-valued ``extra_body``, the two dicts are merged with
    *source* keys winning individual sub-keys rather than replacing the
    entire dict.
    """
    for k, v in source.items():
        if (
            k == "extra_body"
            and isinstance(v, dict)
            and isinstance(target.get(k), dict)
        ):
            target[k] = {**target[k], **v}
        else:
            target[k] = v


def build_aux_kwargs(
    model: AIModel,
    creds_kwargs: dict[str, Any],
    *,
    call_site_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Merge model_parameters, credentials, and call-site kwargs for an aux call.

    Precedence (highest to lowest):
    1. ``call_site_kwargs`` (temperature, max_tokens, messages, model, ...)
    2. ``creds_kwargs``     (api_key, api_base from credential resolution)
    3. ``model.model_parameters`` (operator-set knobs stored on the model row)
    4. safety defaults     (``drop_params=True``, capability-checked reasoning
       disable knob)

    The construction starts with the lowest-priority dict and overwrites
    upward, so a key that appears in both ``model_parameters`` and
    ``call_site_kwargs`` keeps the call-site value. ``extra_body`` dicts are
    deep-merged so a default thinking-disable knob is not clobbered by an
    unrelated extra_body key in a higher layer.

    Preloop client branding is applied last: User-Agent is always Preloop
    (never LiteLLM). OpenRouter also gets ``X-Title`` / ``HTTP-Referer``.
    """
    # Layer 4: safety defaults (capability-checked, not blanket).
    merged: dict[str, Any] = {"drop_params": True}
    merged.update(_get_reasoning_disable_defaults(model))

    # Layer 3: model row's JSONB column.
    model_params = getattr(model, "model_parameters", None)
    if isinstance(model_params, dict):
        _merge_extra_body(merged, model_params)

    # Layer 2: resolved credentials (api_key, api_base).
    _merge_extra_body(merged, creds_kwargs)

    # Layer 1: call-site explicit args (always win).
    _merge_extra_body(merged, call_site_kwargs)

    apply_preloop_client_headers(merged, model)
    return merged


def get_aux_openai_sdk_extra_kwargs(
    model: AIModel,
    *,
    call_site_kwargs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Return extra kwargs for ``openai.OpenAI().chat.completions.create()`` aux calls.

    Merges the model row's ``model_parameters`` with a capability-checked
    reasoning-disable default. Call-site kwargs already passed as named
    arguments take precedence: any key present in ``call_site_kwargs`` is
    excluded from the result so the caller can simply spread the return value
    into the create call without overriding its own explicit arguments.

    Only keys known to be safe named parameters of the OpenAI SDK are included.
    ``reasoning_effort="none"`` is only added when litellm's model map confirms
    the model accepts that level; otherwise no reasoning knob is sent. Models
    routed through OpenRouter get OpenRouter's own disable knob via
    ``extra_body`` (a first-class OpenAI SDK parameter), since reasoning left
    enabled there burns the token budget before any content is produced.
    """
    from preloop.services.litellm_routing import is_openrouter_endpoint

    extras: dict[str, Any] = {}

    provider = (getattr(model, "provider_name", None) or "").strip().lower()
    endpoint = getattr(model, "api_endpoint", None)
    if provider == "openrouter" or is_openrouter_endpoint(endpoint):
        extras["extra_body"] = {"reasoning": {"enabled": False}}

    # Capability-checked default: only add reasoning_effort for models that
    # accept "none". The OpenAI SDK path is always the openai provider.
    identifier = (getattr(model, "model_identifier", None) or "").strip()
    if _model_supports_none_reasoning_effort(identifier, "openai"):
        extras["reasoning_effort"] = "none"

    model_params = getattr(model, "model_parameters", None)
    if isinstance(model_params, dict):
        for k, v in model_params.items():
            if k in _OPENAI_SDK_SAFE_MODEL_PARAM_KEYS:
                extras[k] = v

    # Remove keys the call site already supplies.
    if call_site_kwargs:
        for k in call_site_kwargs:
            extras.pop(k, None)

    return extras


def check_reasoning_model_empty_content(response: Any) -> None:
    """Raise if a reasoning model used all tokens on reasoning and returned nothing.

    A completion with *empty* ``content``, ``finish_reason == "length"``, and a
    non-empty ``reasoning_content`` attribute means the model burned the entire
    token budget on its internal reasoning pass and produced no user-visible
    output. This is a failure for aux generation paths and should trigger a
    fallback retry.

    Raises:
        ReasoningModelEmptyContentError: when the condition is detected.
    """
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return

    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason != "length":
        return

    message = getattr(choice, "message", None)
    if message is None:
        return

    content = getattr(message, "content", None) or ""
    if content.strip():
        return  # has real content, not the failure mode

    # Check for reasoning payload (attribute or dict key).
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning is None and isinstance(getattr(message, "__dict__", None), dict):
        reasoning = message.__dict__.get("reasoning_content")
    if reasoning is None and hasattr(message, "get"):
        reasoning = message.get("reasoning_content")

    if reasoning and str(reasoning).strip():
        raise ReasoningModelEmptyContentError(
            "Model exhausted token budget on reasoning; content is empty "
            "(finish_reason=length)."
        )


async def call_with_default_model_fallback(
    *,
    db: Session,
    account_id: str,
    primary_model: AIModel,
    caller: Callable[[AIModel, dict[str, Any]], Any],
    operation_name: str,
    attempt_timeout: Optional[float] = None,
) -> Any:
    """
    Call an AI model with automatic fallback to the system-wide default on failure.

    This orchestrator attempts the call with the primary model. On failure (auth error,
    provider error, timeout), it retries ONCE with the system-wide default model, subject
    to a per-account daily cap. If the fallback also fails or no system default exists,
    the result is None (graceful degradation).

    The daily cap and warning dedupe are tracked in-process only (see module docstring).

    Args:
        db: Database session.
        account_id: Account identifier (for cap tracking and logging).
        primary_model: The account's preferred model to try first.
        caller: A callable that takes (model: AIModel, creds_kwargs: dict) and performs
                the generation. It should raise on failure.
        operation_name: Human-readable operation name for logging (e.g., "approval_summary").
        attempt_timeout: Optional per-attempt timeout in seconds. Each attempt gets its
                own budget, so a slow primary still leaves room for the fallback. Callers
                that wrap this in an overall deadline should pass roughly half of it.

    Returns:
        The result of the caller, or None if both primary and fallback fail.
    """

    def _resolve_and_call(model: AIModel) -> Any:
        # Resolve inside the worker thread: a secret backend lookup can do
        # network I/O and must never run on the event loop. Retry transient
        # provider 429s here so a blip does not burn the one-shot fallback.
        return call_with_aux_retry(
            lambda: caller(model, resolve_model_call_credentials(model, db=db)),
            operation_name=operation_name,
        )

    async def _attempt(model: AIModel) -> Any:
        coro = asyncio.to_thread(_resolve_and_call, model)
        if attempt_timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=attempt_timeout)

    # Try the primary model first.
    try:
        return await _attempt(primary_model)
    except Exception as primary_exc:
        logger.info(
            "Primary model %s (%s) failed for %s on account %s: %s. Checking fallback.",
            primary_model.id,
            primary_model.model_identifier,
            operation_name,
            account_id,
            type(primary_exc).__name__,
        )

        # Check if we should attempt fallback. Keyed on the UTC day so the
        # window does not shift with the server's local timezone.
        today = datetime.now(UTC).date()
        cap_key = (account_id, today)
        daily_cap = get_aux_fallback_daily_cap()

        with _fallback_counters_lock:
            current_count = _fallback_counters[cap_key]
            if current_count >= daily_cap:
                logger.warning(
                    "Account %s has reached daily aux fallback cap (%d). Skipping fallback for %s.",
                    account_id,
                    daily_cap,
                    operation_name,
                )
                return None

        # Get the system-wide default model.
        system_default = crud_ai_model.get_default_active_model(
            db, account_id=None, model_kind="llm"
        )

        if not system_default:
            logger.info(
                "No system-wide default model configured. Cannot fallback for %s on account %s.",
                operation_name,
                account_id,
            )
            return None

        if system_default.id == primary_model.id:
            logger.info(
                "System default model is the same as the failed primary model (%s). Skipping fallback.",
                primary_model.id,
            )
            return None

        # Claim a slot under the same lock that guards the cap, so concurrent
        # callers cannot both pass the check above and overshoot the cap.
        with _fallback_counters_lock:
            _prune_stale_days(_fallback_counters, today)
            if _fallback_counters[cap_key] >= daily_cap:
                logger.warning(
                    "Account %s reached the daily aux fallback cap (%d) concurrently. "
                    "Skipping fallback for %s.",
                    account_id,
                    daily_cap,
                    operation_name,
                )
                return None
            _fallback_counters[cap_key] += 1

        # Emit deduped warning.
        with _fallback_warnings_lock:
            _prune_stale_days(_fallback_warnings, today)
            if not _fallback_warnings[cap_key]:
                logger.warning(
                    "Account %s is riding the aux model fallback (primary model %s failed). "
                    "First failure: %s for %s.",
                    account_id,
                    primary_model.model_identifier,
                    type(primary_exc).__name__,
                    operation_name,
                )
                _fallback_warnings[cap_key] = True

        # Try the fallback model.
        try:
            logger.info(
                "Attempting fallback to system default model %s (%s) for %s on account %s.",
                system_default.id,
                system_default.model_identifier,
                operation_name,
                account_id,
            )
            result = await _attempt(system_default)
            logger.info(
                "Fallback to system default model succeeded for %s on account %s.",
                operation_name,
                account_id,
            )
            return result
        except Exception as fallback_exc:
            logger.warning(
                "Fallback model %s (%s) also failed for %s on account %s: %s. Returning None.",
                system_default.id,
                system_default.model_identifier,
                operation_name,
                account_id,
                type(fallback_exc).__name__,
            )
            return None
