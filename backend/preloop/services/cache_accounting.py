"""Per-request and per-session prompt-cache accounting for the session view.

The gateway already persists an exact, provider-sourced cache split on every
``ApiUsage`` row (``cache_read_tokens`` / ``cache_creation_tokens``, written by
``OpenAIGatewayService._extract_token_details``). The local estimation fallback
never fabricates that split, so a cache column is either a provider's number or
NULL. This module turns those columns into something a UI can render **without
lying**:

* a NULL cache column is reported as ``None`` ("not reported by provider"),
  never as ``0``;
* a cache miss is ``reported`` when the provider actually sent a miss count
  (DeepSeek's ``prompt_cache_miss_tokens``, which otherwise sits unused in
  ``meta_data["usage_details"]``), ``derived`` when we subtract a reported read
  from the reported prompt total, and ``None`` when neither is possible;
* estimated cache savings are computed only from explicit catalog cache prices.
  Models without an exact catalog price never contribute an approximation: the
  savings figure is either exact for every contributing model
  (``catalog_exact``), an exact-but-partial lower bound when some models are
  unpriced (``catalog_exact_partial``), or omitted with a reason.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# Provenance of a per-request cache-miss number.
CACHE_MISS_SOURCE_REPORTED = "reported"
CACHE_MISS_SOURCE_DERIVED = "derived"

# Why a session-level savings figure was withheld.
SAVINGS_OMITTED_NO_CACHE_READS = "no_cache_reads"
SAVINGS_OMITTED_NO_CATALOG_PRICE = "no_catalog_cache_price"

# Basis labels for a savings number we are willing to show. ``catalog_exact``
# means every model that contributed cache reads had explicit catalog prices;
# ``catalog_exact_partial`` means the figure covers only the priced models and
# at least one contributing model was skipped for lack of a catalog price.
SAVINGS_BASIS_CATALOG_EXACT = "catalog_exact"
SAVINGS_BASIS_CATALOG_EXACT_PARTIAL = "catalog_exact_partial"

_PRICE_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "model_prices.json"


@lru_cache(maxsize=1)
def _price_catalog() -> dict[str, Any]:
    try:
        loaded: Any = json.loads(_PRICE_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A missing or corrupted catalog silently disables the savings figure
        # for every model, so make the degradation visible to operators.
        logger.warning(
            "Failed to load model price catalog from %s; cache savings will "
            "be omitted for all models.",
            _PRICE_CATALOG_PATH,
            exc_info=True,
        )
        return {}
    if not isinstance(loaded, dict):
        logger.warning(
            "Model price catalog at %s is not a JSON object; cache savings "
            "will be omitted for all models.",
            _PRICE_CATALOG_PATH,
        )
        return {}
    return loaded


def _as_int(value: Any) -> Optional[int]:
    """Coerce a provider-reported token count to int, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_details(meta_data: Any) -> Mapping[str, Any]:
    if not isinstance(meta_data, Mapping):
        return {}
    details = meta_data.get("usage_details")
    return details if isinstance(details, Mapping) else {}


def _prompt_tokens_details(usage_details: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = usage_details.get("prompt_tokens_details")
    return nested if isinstance(nested, Mapping) else {}


def reported_cache_miss_tokens(meta_data: Any) -> Optional[int]:
    """Return a provider-reported cache MISS token count, if one exists.

    Today only DeepSeek reports misses directly (``prompt_cache_miss_tokens``).
    litellm maps its hit count onto ``cached_tokens`` but drops the miss count,
    which survives verbatim in ``meta_data["usage_details"]``. This promotes it.

    Args:
        meta_data: The ``ApiUsage.meta_data`` JSONB value.

    Returns:
        The reported miss token count, or ``None`` when the provider sent none.
    """
    details = _usage_details(meta_data)
    if not details:
        return None
    nested = _prompt_tokens_details(details)
    for candidate in (
        details.get("prompt_cache_miss_tokens"),
        nested.get("prompt_cache_miss_tokens"),
        details.get("cache_miss_input_tokens"),
    ):
        parsed = _as_int(candidate)
        if parsed is not None:
            return max(parsed, 0)
    return None


def _fallback_cache_read(details: Mapping[str, Any]) -> Optional[int]:
    nested = _prompt_tokens_details(details)
    for candidate in (
        nested.get("cached_tokens"),
        details.get("cache_read_input_tokens"),
    ):
        parsed = _as_int(candidate)
        if parsed is not None:
            return max(parsed, 0)
    return None


def _fallback_cache_creation(details: Mapping[str, Any]) -> Optional[int]:
    nested = _prompt_tokens_details(details)
    for candidate in (
        nested.get("cache_creation_tokens"),
        details.get("cache_creation_input_tokens"),
    ):
        parsed = _as_int(candidate)
        if parsed is not None:
            return max(parsed, 0)
    return None


@dataclass
class RequestCacheAccounting:
    """Cache accounting for one gateway request, with honest absences."""

    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    cache_miss_tokens: Optional[int] = None
    cache_miss_source: Optional[str] = None
    # True when the provider reported anything at all about this call's cache.
    has_cache_data: bool = False
    # provider | estimated | partial | imported | None (legacy rows).
    usage_source: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_miss_source": self.cache_miss_source,
            "has_cache_data": self.has_cache_data,
            "usage_source": self.usage_source,
        }


def build_request_cache_accounting(row: Any) -> RequestCacheAccounting:
    """Build the per-call cache view for one ``ApiUsage`` row.

    Reads the authoritative first-class columns and falls back to the verbatim
    provider payload in ``meta_data["usage_details"]`` only for rows written
    before those columns existed. Nothing is inferred beyond the arithmetic
    identity ``miss = prompt - read - write``, and that only when the read side
    was actually reported.

    Args:
        row: An ``ApiUsage`` ORM row (or any object with the same attributes).

    Returns:
        A :class:`RequestCacheAccounting` where ``None`` means "not reported".
    """
    meta_data = getattr(row, "meta_data", None)
    details = _usage_details(meta_data)

    read = _as_int(getattr(row, "cache_read_tokens", None))
    if read is None:
        read = _fallback_cache_read(details)
    creation = _as_int(getattr(row, "cache_creation_tokens", None))
    if creation is None:
        creation = _fallback_cache_creation(details)

    reported_miss = reported_cache_miss_tokens(meta_data)
    miss: Optional[int] = None
    miss_source: Optional[str] = None
    if reported_miss is not None:
        miss = reported_miss
        miss_source = CACHE_MISS_SOURCE_REPORTED
    elif read is not None:
        prompt_tokens = _as_int(getattr(row, "prompt_tokens", None))
        if prompt_tokens is not None:
            miss = max(prompt_tokens - read - (creation or 0), 0)
            miss_source = CACHE_MISS_SOURCE_DERIVED

    return RequestCacheAccounting(
        cache_read_tokens=read,
        cache_creation_tokens=creation,
        cache_miss_tokens=miss,
        cache_miss_source=miss_source,
        has_cache_data=read is not None or creation is not None,
        usage_source=getattr(row, "usage_source", None),
    )


def _catalog_cache_read_prices_per_1k(
    *, model_alias: Optional[str], provider_name: Optional[str]
) -> tuple[Optional[float], Optional[float]]:
    """Look up EXACT catalog input and cache-read prices, per 1k tokens.

    Deliberately narrower than
    ``preloop.services.context_analysis.resolve_cache_prices_per_1k``: that
    helper synthesizes a cache price from an input price times a per-provider
    multiplier when the catalog is silent. A synthesized price is fine for a
    relative "this rewrite cost extra" signal but not for a dollar figure shown
    as cache savings, so this resolver returns ``(None, None)`` instead of
    guessing.

    Returns:
        ``(input_price_per_1k, cache_read_price_per_1k)``; both ``None`` when
        the catalog carries no explicit pair for the model.
    """
    catalog = _price_catalog()
    if not catalog:
        return None, None
    candidates: list[str] = []
    alias = (model_alias or "").strip()
    if alias:
        candidates.append(alias)
        if "/" in alias:
            bare = alias.split("/", 1)[1]
            candidates.append(bare)
            candidates.append(alias.replace("/", "."))
        provider = (provider_name or "").strip().lower()
        if provider and alias:
            bare = alias.split("/", 1)[1] if "/" in alias else alias
            candidates.append(f"{provider}/{bare}")
            candidates.append(f"{provider}.{bare}")
    for candidate in candidates:
        entry = catalog.get(candidate)
        if not isinstance(entry, dict):
            continue
        input_cost = entry.get("input_cost_per_token")
        read_cost = entry.get("cache_read_input_token_cost")
        if input_cost is None or read_cost is None:
            continue
        try:
            return float(input_cost) * 1000.0, float(read_cost) * 1000.0
        except (TypeError, ValueError):
            continue
    return None, None


@dataclass
class SessionCacheModelGroup:
    """One (model, provider) group inside a session cache rollup."""

    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    requests: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    prompt_tokens: int = 0
    write_reported: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_alias": self.model_alias,
            "provider_name": self.provider_name,
            "requests": self.requests,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "prompt_tokens": self.prompt_tokens,
            "write_reported": self.write_reported,
        }


@dataclass
class SessionCacheSummary:
    """Whole-session prompt-cache rollup with explicit coverage and honesty."""

    requests_total: int = 0
    # Requests where the provider reported at least one cache number.
    requests_with_cache_data: int = 0
    requests_without_cache_data: int = 0
    # Prompt tokens of the covered requests only; the ratio's denominator.
    covered_prompt_tokens: int = 0
    uncovered_prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    uncached_prompt_tokens: int = 0
    # None when NO provider in this session reports cache writes at all
    # (OpenAI/Gemini/DeepSeek have no billable write concept) — not zero.
    cache_write_tokens: Optional[int] = None
    cache_hit_ratio: Optional[float] = None
    estimated_cache_savings_usd: Optional[float] = None
    savings_basis: Optional[str] = None
    savings_omitted_reason: Optional[str] = None
    # Covered requests whose provider reported no prompt total at all: their
    # prompt tokens are UNKNOWN, not zero, so they are counted here instead of
    # being folded into ``covered_prompt_tokens`` as 0.
    requests_with_unknown_prompt_tokens: int = 0
    models: list[SessionCacheModelGroup] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests_total": self.requests_total,
            "requests_with_cache_data": self.requests_with_cache_data,
            "requests_without_cache_data": self.requests_without_cache_data,
            "covered_prompt_tokens": self.covered_prompt_tokens,
            "uncovered_prompt_tokens": self.uncovered_prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "uncached_prompt_tokens": self.uncached_prompt_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit_ratio": self.cache_hit_ratio,
            "estimated_cache_savings_usd": self.estimated_cache_savings_usd,
            "savings_basis": self.savings_basis,
            "savings_omitted_reason": self.savings_omitted_reason,
            "requests_with_unknown_prompt_tokens": (
                self.requests_with_unknown_prompt_tokens
            ),
            "models": [group.as_dict() for group in self.models],
        }


def summarize_session_cache(rows: Iterable[Any]) -> SessionCacheSummary:
    """Roll per-request cache accounting up to one session-level summary.

    The hit ratio is computed **only over requests whose provider reported a
    cache split**, and the uncovered prompt tokens are reported alongside it so
    a caller can state the coverage rather than silently averaging blind rows
    in as misses.

    Savings are the difference between paying the full input price and the
    cache-read price for the tokens actually served from cache, and are emitted
    only when every contributing model has explicit catalog prices for both.

    Args:
        rows: ``ApiUsage`` rows for one runtime session.

    Returns:
        A :class:`SessionCacheSummary`.
    """
    summary = SessionCacheSummary()
    groups: dict[tuple[Optional[str], Optional[str]], SessionCacheModelGroup] = {}
    any_write_reported = False
    write_total = 0

    for row in rows:
        summary.requests_total += 1
        accounting = build_request_cache_accounting(row)
        # None means the provider reported NO prompt total, which is not the
        # same claim as zero prompt tokens; such rows are counted separately
        # instead of contributing 0 to the prompt-token sums.
        prompt_tokens = _as_int(getattr(row, "prompt_tokens", None))
        if not accounting.has_cache_data:
            summary.requests_without_cache_data += 1
            if prompt_tokens is None:
                summary.requests_with_unknown_prompt_tokens += 1
            else:
                summary.uncovered_prompt_tokens += prompt_tokens
            continue

        summary.requests_with_cache_data += 1
        if prompt_tokens is None:
            summary.requests_with_unknown_prompt_tokens += 1
        else:
            summary.covered_prompt_tokens += prompt_tokens
        read = accounting.cache_read_tokens or 0
        creation = accounting.cache_creation_tokens
        summary.cached_prompt_tokens += read
        if accounting.cache_miss_tokens is not None:
            summary.uncached_prompt_tokens += accounting.cache_miss_tokens
        if creation is not None:
            any_write_reported = True
            write_total += creation

        key = (
            getattr(row, "model_alias", None),
            getattr(row, "provider_name", None),
        )
        group = groups.get(key)
        if group is None:
            group = SessionCacheModelGroup(model_alias=key[0], provider_name=key[1])
            groups[key] = group
        group.requests += 1
        group.prompt_tokens += prompt_tokens or 0
        group.cache_read_tokens += read
        if creation is not None:
            group.write_reported = True
            group.cache_creation_tokens += creation

    summary.cache_write_tokens = write_total if any_write_reported else None
    summary.models = list(groups.values())

    denominator = summary.cached_prompt_tokens + summary.uncached_prompt_tokens
    if denominator > 0:
        summary.cache_hit_ratio = round(summary.cached_prompt_tokens / denominator, 4)

    savings, basis, omitted = _session_cache_savings(summary.models)
    summary.estimated_cache_savings_usd = savings
    summary.savings_basis = basis
    summary.savings_omitted_reason = omitted
    return summary


def _session_cache_savings(
    models: Iterable[SessionCacheModelGroup],
) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Compute exact-only cache savings across the session's models.

    Models without an exact catalog price never contribute an approximated
    figure. When SOME models are priced and some are not, the priced subtotal
    is still returned, labelled ``catalog_exact_partial`` so the UI can state
    that the figure is a lower bound rather than dropping it entirely.
    """
    total = 0.0
    contributing = 0
    unpriced = 0
    for group in models:
        if group.cache_read_tokens <= 0:
            continue
        contributing += 1
        input_per_1k, read_per_1k = _catalog_cache_read_prices_per_1k(
            model_alias=group.model_alias, provider_name=group.provider_name
        )
        if input_per_1k is None or read_per_1k is None:
            unpriced += 1
            continue
        differential = input_per_1k - read_per_1k
        if differential <= 0:
            continue
        total += (group.cache_read_tokens / 1000.0) * differential
    if contributing == 0:
        return None, None, SAVINGS_OMITTED_NO_CACHE_READS
    if unpriced == contributing:
        return None, None, SAVINGS_OMITTED_NO_CATALOG_PRICE
    basis = (
        SAVINGS_BASIS_CATALOG_EXACT_PARTIAL if unpriced else SAVINGS_BASIS_CATALOG_EXACT
    )
    return round(total, 6), basis, None
