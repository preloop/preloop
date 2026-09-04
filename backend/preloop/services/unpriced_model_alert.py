"""Admin alerting for gateway traffic the price catalog cannot price.

When the gateway records usage with ``cost_source='unpriced'`` the tokens are
metered but no cost can be attributed, so the user sees no spend for real
spend. That is a silent failure of Preloop's core promise, and in production
1667 such calls accumulated before anyone noticed.

This module notifies admins the first time a given ``(model_alias, provider)``
pair proves unpriceable, then stays quiet for :data:`ALERT_COOLDOWN_HOURS`.

Two properties matter because this runs on the hot gateway path:

- **Cross-replica dedup.** The cooldown marker is an ``audit_log`` row, not
  process memory. Production runs several API and gateway replicas, and a
  previous incident was caused by an in-memory-only throttle firing once per
  replica. The in-process cache here is only a fast path in front of the
  persisted marker.
- **Never fails the request.** Every database and notification error is caught
  and logged. A pricing-visibility alert must never turn into a user-facing
  gateway error.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log
from preloop.models.models.ai_model import AIModel
from preloop.services.litellm_routing import (
    OPENAI_COMPATIBLE_PROVIDERS,
    is_openrouter_endpoint,
)
from preloop.services.model_runtime_resolver import gateway_model_alias_candidates
from preloop.sync.tasks import notify_admins

logger = logging.getLogger(__name__)

#: Audit action used as the persisted, cross-replica dedup marker.
UNPRICED_ALERT_ACTION = "unpriced_model_alert"

#: Quiet period per (model_alias, provider) after an alert is sent.
ALERT_COOLDOWN_HOURS = int(os.getenv("UNPRICED_MODEL_ALERT_COOLDOWN_HOURS", "24"))

#: DashScope hosts we catalog. openai-compatible configs pointed here are
#: still a shared-catalog hole and should page; home LiteLLM / OpenCode Zen
#: / other customer endpoints should not.
_DASHSCOPE_HOSTS = frozenset(
    {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    }
)

# Fast path only; the audit-log marker remains authoritative across replicas.
_local_lock = threading.Lock()
_local_recent: dict[str, float] = {}


def _upstream_provider(ai_model: AIModel, provider_name: Optional[str]) -> str:
    """Best-effort name of the upstream actually serving the model.

    Different AIModel configs for one upstream model can carry different
    provider names — the production Auto Router incident had it registered as
    both ``openrouter`` and ``openai-compatible`` (pointed at the OpenRouter
    endpoint). Normalising every OpenRouter-fronted config to ``openrouter``
    keeps those spellings on one dedup key while still separating true
    different upstreams (e.g. OpenRouter-routed vs direct-vendor).
    """
    provider = (ai_model.provider_name or provider_name or "unknown").strip().lower()
    if provider == "openrouter" or is_openrouter_endpoint(ai_model.api_endpoint):
        return "openrouter"
    return provider


def _dedupe_key(
    model_alias: str,
    provider_name: Optional[str],
    ai_model: Optional[AIModel] = None,
) -> str:
    """Build the per-model dedup key.

    One gateway model is addressable under several alias spellings
    (``openrouter/auto-beta``, ``openai-compatible/openrouter/auto-beta``,
    ``openrouter/openrouter/auto-beta``), and different AIModel configs for
    the same upstream model may even carry different provider names. Keying
    on the raw ``(provider, alias)`` pair fired one alert per spelling. When
    the resolved model is available, the key is instead the canonical
    spelling from :func:`gateway_model_alias_candidates` — the shortest
    candidate, which by the resolver's suffix-match rule is the bare tail all
    spellings share — so every spelling lands on one cooldown marker.

    The canonical key is prefixed with the (normalised) upstream provider so
    two genuinely different models that happen to share a bare alias tail
    across providers (``openrouter/deepseek/deepseek-chat`` vs a direct
    ``deepseek/deepseek-chat`` config) do not swallow each other's alerts.
    OpenRouter-fronted configs are normalised to one prefix regardless of
    their recorded provider name (see :func:`_upstream_provider`).

    Args:
        model_alias: Recorded model alias that could not be priced.
        provider_name: Provider that served the request.
        ai_model: The resolved model, when the caller has it.

    Returns:
        A stable key identifying the model across its alias spellings.
    """
    if ai_model is not None:
        try:
            candidates = gateway_model_alias_candidates(ai_model)
        except Exception:  # noqa: BLE001 - canonicalisation must never break dedup
            candidates = set()
        if candidates:
            canonical = min(candidates, key=lambda c: (len(c), c))
            upstream = _upstream_provider(ai_model, provider_name)
            return f"model|{upstream}|{canonical.strip().lower()}"
    return f"{(provider_name or 'unknown').strip().lower()}|{model_alias.strip()}"


def _endpoint_host(endpoint: Optional[str]) -> str:
    """Return the lowercase hostname of an endpoint URL, or "" if unparseable."""
    if not isinstance(endpoint, str):
        return ""
    raw = endpoint.strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = f"//{raw}"
    try:
        return (urlsplit(raw).hostname or "").lower()
    except ValueError:  # pragma: no cover - malformed URLs
        return ""


def _is_cataloged_marketplace_endpoint(endpoint: Optional[str]) -> bool:
    """True when this endpoint is a host whose prices we maintain."""
    if is_openrouter_endpoint(endpoint):
        return True
    host = _endpoint_host(endpoint)
    if not host:
        return False
    return any(
        host == known or host.endswith(f".{known}") for known in _DASHSCOPE_HOSTS
    )


def should_page_unpriced_model(ai_model: Optional[AIModel]) -> bool:
    """Return False when paging an admin cannot fix the catalog.

    ``openai-compatible`` / ``custom`` models on a customer-owned endpoint
    (home LiteLLM, OpenCode Zen, a private proxy) will never appear in the
    shared price snapshot. The Attention page still lists them as unpriced
    so the account can set an override. OpenRouter- and DashScope-fronted
    configs still page, because those hosts are ones we catalog.

    Args:
        ai_model: The resolved model. ``None`` keeps the previous contract
            (page), used by callers that only have the alias.

    Returns:
        True when ``notify_unpriced_model`` should still run.
    """
    if ai_model is None:
        return True
    provider = (ai_model.provider_name or "").strip().lower()
    if provider not in OPENAI_COMPATIBLE_PROVIDERS:
        return True
    return _is_cataloged_marketplace_endpoint(ai_model.api_endpoint)


def should_notify_unpriced_model(
    *,
    usage_accounting_requested: bool,
    usage_details: Optional[Dict[str, Any]],
    completion_tokens: int,
    ai_model: Optional[AIModel] = None,
) -> bool:
    """Return False when an unpriced row should not page admins.

    When we asked OpenRouter for usage accounting and the response has
    no completion tokens and no ``cost`` / ``cost_details`` fields, the
    request produced no billable output. The row may stay unpriced; do
    not ask an admin to add catalog pricing. Prompt plus completion
    with no cost and no catalog still alerts.

    Customer-owned OpenAI-compatible endpoints are also skipped: see
    :func:`should_page_unpriced_model`.

    Args:
        usage_accounting_requested: True when this request asked
            OpenRouter for usage accounting.
        usage_details: Raw provider usage dict from the response.
        completion_tokens: Completion tokens on the recorded row.
        ai_model: The resolved model, when the caller has it.

    Returns:
        True when ``notify_unpriced_model`` should still run.
    """
    if not should_page_unpriced_model(ai_model):
        return False
    if not usage_accounting_requested:
        return True
    if int(completion_tokens or 0) != 0:
        return True
    if isinstance(usage_details, dict) and (
        "cost" in usage_details or "cost_details" in usage_details
    ):
        return True
    return False


def reset_alert_state_for_tests() -> None:
    """Clear the in-process fast-path cache (test isolation only)."""
    with _local_lock:
        _local_recent.clear()


def _recently_alerted_locally(key: str, cooldown_seconds: float) -> bool:
    """Return True when this process alerted for ``key`` inside the cooldown."""
    now = time.monotonic()
    with _local_lock:
        last = _local_recent.get(key)
        return last is not None and now - last < cooldown_seconds


def _mark_locally(key: str) -> None:
    """Record a local alert timestamp for the fast path."""
    with _local_lock:
        _local_recent[key] = time.monotonic()


def _recently_alerted_in_db(
    db: Session, *, account_id: str, key: str, cooldown_hours: int
) -> bool:
    """Return True when any replica already alerted for ``key`` recently.

    Args:
        db: Database session.
        account_id: Account whose traffic was unpriced.
        key: Dedup key for the model/provider pair.
        cooldown_hours: Quiet period length.

    Returns:
        True when a marker newer than the cooldown exists.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    existing = crud_audit_log.get_by_account(
        db,
        account_id=account_id,
        action=UNPRICED_ALERT_ACTION,
        resource_type="model_pricing",
        start_date=since,
        limit=200,
    )
    return any(entry.resource_id == key for entry in existing)


def notify_unpriced_model(
    db: Session,
    *,
    account_id: str,
    model_alias: str,
    provider_name: Optional[str],
    total_tokens: int,
    cooldown_hours: Optional[int] = None,
    ai_model: Optional[AIModel] = None,
) -> bool:
    """Alert admins that a model could not be priced, at most once per cooldown.

    Safe to call on every unpriced gateway request: repeats inside the cooldown
    are dropped, and all failures are swallowed so the caller's request is
    never affected.

    Args:
        db: Database session.
        account_id: Account whose usage could not be priced.
        model_alias: Model alias recorded on the usage row.
        provider_name: Provider that served the request.
        total_tokens: Tokens on the triggering request, for context.
        cooldown_hours: Override for the quiet period.
        ai_model: The resolved model; enables alias-canonical dedup so
            multiple spellings of one model share a single cooldown.

    Returns:
        True when a notification was actually sent.
    """
    if not model_alias or not account_id:
        return False

    window = ALERT_COOLDOWN_HOURS if cooldown_hours is None else cooldown_hours
    key = _dedupe_key(model_alias, provider_name, ai_model)

    try:
        if _recently_alerted_locally(key, window * 3600):
            return False
        if _recently_alerted_in_db(
            db, account_id=account_id, key=key, cooldown_hours=window
        ):
            _mark_locally(key)
            return False

        # Claim the cooldown BEFORE notifying so a notification failure cannot
        # turn into a retry storm on the next request.
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            action=UNPRICED_ALERT_ACTION,
            resource_type="model_pricing",
            resource_id=key,
            status="success",
            details={
                "model_alias": model_alias,
                "provider_name": provider_name,
                "total_tokens": total_tokens,
            },
        )
        _mark_locally(key)
    except Exception:  # noqa: BLE001 - alerting must never break the gateway
        logger.exception(
            "Unpriced-model alert bookkeeping failed for provider %s",
            provider_name,
        )
        return False

    subject = f"Preloop: no pricing for model {model_alias}"
    message = (
        "Preloop metered gateway usage it could not price, so this traffic "
        "shows no cost for the customer.\n\n"
        f"Model alias: {model_alias}\n"
        f"Provider: {provider_name or 'unknown'}\n"
        f"Account: {account_id}\n"
        f"Tokens on triggering request: {total_tokens:,}\n\n"
        "Add pricing for this model (or a per-account price override), then "
        "reprice historical rows with the usage repricing task so the "
        "customer's dashboard becomes accurate retroactively.\n"
        f"Further alerts for this model are suppressed for {window}h."
    )

    try:
        notify_admins(subject, message)
    except Exception:  # noqa: BLE001 - never raise into the request path
        logger.exception("Failed to send unpriced-model notification")
        return False
    return True
