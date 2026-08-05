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
        # network I/O and must never run on the event loop.
        return caller(model, resolve_model_call_credentials(model, db=db))

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
