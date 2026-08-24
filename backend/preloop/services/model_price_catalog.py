"""Vendored model-price catalog loader and live price lookup.

Preloop vendors a filtered snapshot of litellm's public price map at
``services/data/model_prices.json`` (regenerated with
``scripts/update_model_prices.py``). Loading it at startup makes default
pricing deterministic per release instead of depending on whichever litellm
version happens to be installed.

The snapshot is deliberately small (current chat models only), so a model can
legitimately be missing from it. When the gateway records an ``unpriced``
usage row, :func:`schedule_price_lookup` fetches the model's price from the
live upstream map ONCE via a bounded background pool, registers it with
litellm, and re-prices the triggering row. Lookups are throttled hard:

- the downloaded upstream map is cached in-process for ``_REMOTE_TTL_SECONDS``,
- failed downloads back off for ``_REMOTE_FAILURE_BACKOFF_SECONDS``,
- model names that the upstream map does not contain enter a negative cache
  for ``_NEGATIVE_TTL_SECONDS`` so the same unknown model never triggers
  repeated work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "model_prices.json"
META_KEY = "_preloop_meta"
# Default follows litellm's published map on GitHub. Prefer pinning a content
# hash via MODEL_PRICE_MAP_SHA256 so a compromised upstream revision cannot
# silently change pricing behavior.
REMOTE_PRICE_MAP_URL = os.getenv(
    "MODEL_PRICE_MAP_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json",
)
REMOTE_PRICE_MAP_SHA256 = os.getenv("MODEL_PRICE_MAP_SHA256", "").strip().lower()

_REMOTE_TTL_SECONDS = int(os.getenv("MODEL_PRICE_MAP_TTL_SECONDS", str(6 * 3600)))
_REMOTE_FAILURE_BACKOFF_SECONDS = int(
    os.getenv("MODEL_PRICE_MAP_FAILURE_BACKOFF_SECONDS", str(15 * 60))
)
_NEGATIVE_TTL_SECONDS = int(
    os.getenv("MODEL_PRICE_MAP_NEGATIVE_TTL_SECONDS", str(24 * 3600))
)
_REMOTE_FETCH_RETRIES = int(os.getenv("MODEL_PRICE_MAP_FETCH_RETRIES", "2"))

# OpenRouter publishes authoritative per-token prices for every model it
# routes, including marketplace snapshots (e.g. dated DeepSeek builds) that
# litellm's map does not carry. Used as a secondary source when the primary
# map misses an ``openrouter/``-prefixed candidate.
OPENROUTER_MODELS_URL = os.getenv(
    "OPENROUTER_MODELS_URL", "https://openrouter.ai/api/v1/models"
)
_OPENROUTER_PREFIX = "openrouter/"
# Z.ai has no equivalent. Probed 2026-08-22 with a live key:
# GET /api/paas/v4/models -> 200 {data, object}; item keys
# created/id/object/owned_by (no pricing). GET /api/paas/v4/pricing,
# /prices, /price -> 404 {error, path, status, timestamp}.
# GET /api/v1/pricing -> HTTP 200 {code: 500, success: false,
# msg: "404 NOT_FOUND"}. Do not invent a live z.ai price fetch.

_lock = threading.Lock()
_loaded = False
_metadata: Optional[Dict[str, Any]] = None

# Live-lookup state (per process). All guarded by _lookup_lock.
_lookup_lock = threading.Lock()


@dataclass
class _PriceMapCache:
    """In-process cache for one remote price map, with failure backoff.

    Both price sources (litellm's published map and OpenRouter's model list)
    follow the same rules: serve the cached map for ``_REMOTE_TTL_SECONDS``,
    and after a failed download refuse to retry for
    ``_REMOTE_FAILURE_BACKOFF_SECONDS`` so an unreachable source cannot be
    hammered once per unpriced request. All state is guarded by the module's
    ``_lookup_lock``.

    Attributes:
        label: Human-readable source name, used in log messages.
    """

    label: str
    _map: Optional[Dict[str, Any]] = field(default=None, repr=False)
    _fetched_at: float = field(default=0.0, repr=False)
    _failed_at: float = field(default=0.0, repr=False)

    def get(
        self, fetch: Callable[[], Optional[Dict[str, Any]]]
    ) -> Optional[Dict[str, Any]]:
        """Return the cached map, downloading it via ``fetch`` when stale.

        Args:
            fetch: Callable performing the actual download. It must return
                None for any failure (network, validation, empty payload);
                that outcome starts the failure backoff window.

        Returns:
            The cached or freshly downloaded map, or None when a backoff is in
            effect or the download failed.
        """
        now = time.monotonic()
        with _lookup_lock:
            if self._map is not None and now - self._fetched_at < _REMOTE_TTL_SECONDS:
                return self._map
            if self._failed_at and now - self._failed_at < (
                _REMOTE_FAILURE_BACKOFF_SECONDS
            ):
                return None

        fetched = fetch()
        if fetched is None:
            self.mark_failed()
            return None
        self.set(fetched)
        return fetched

    def set(self, price_map: Dict[str, Any]) -> None:
        """Store a freshly downloaded map and clear any failure backoff."""
        with _lookup_lock:
            self._map = price_map
            self._fetched_at = time.monotonic()
            self._failed_at = 0.0

    def mark_failed(self) -> None:
        """Start the failure backoff window after a failed download."""
        with _lookup_lock:
            self._failed_at = time.monotonic()

    def reset(self) -> None:
        """Drop the cached map and any backoff state (test isolation only)."""
        with _lookup_lock:
            self._map = None
            self._fetched_at = 0.0
            self._failed_at = 0.0

    def snapshot(self) -> Dict[str, Any]:
        """Return the cache's current state for tests and diagnostics."""
        with _lookup_lock:
            return {
                "map": self._map,
                "fetched_at": self._fetched_at,
                "failed_at": self._failed_at,
            }


# litellm's published map; the primary source for every model.
_remote_cache = _PriceMapCache("litellm")
# OpenRouter's own model list; secondary source for marketplace-routed models.
_openrouter_cache = _PriceMapCache("openrouter")

# candidate name -> monotonic time of the failed lookup (negative cache).
_negative_cache: Dict[str, float] = {}
# candidate names with a lookup currently in flight.
_pending_lookups: set[str] = set()
# Bound concurrent live lookups so unknown models cannot spawn unbounded threads.
_LOOKUP_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="price-lookup")


def _model_log_token(model_name: str) -> str:
    """Return a stable log token that never embeds the raw model name."""
    digest = hashlib.sha256(model_name.encode("utf-8", errors="replace")).hexdigest()
    return f"model#{digest[:12]}"


def load_catalog(path: Optional[Path] = None, *, force: bool = False) -> bool:
    """Register the vendored price snapshot with litellm.

    Merges the snapshot into ``litellm.model_cost`` via
    ``litellm.register_model`` (per-key override; models missing from the
    snapshot keep litellm's bundled prices). Idempotent per process.

    Args:
        path: Snapshot path override (tests); defaults to the vendored file.
        force: Reload even if already loaded in this process.

    Returns:
        True when the catalog was (re)loaded, False when skipped or failed.
    """
    global _loaded, _metadata
    catalog_path = path or CATALOG_PATH
    with _lock:
        if _loaded and not force:
            return False
        try:
            raw = json.loads(catalog_path.read_text())
        except FileNotFoundError:
            logger.warning(
                "Model price catalog missing at %s; falling back to litellm's "
                "bundled prices",
                catalog_path,
            )
            return False
        except (OSError, ValueError):
            logger.exception("Failed to read model price catalog %s", catalog_path)
            return False

        metadata = raw.pop(META_KEY, None)
        if not raw:
            logger.warning("Model price catalog %s is empty", catalog_path)
            return False

        try:
            import litellm

            litellm.register_model(raw)
            # Sanity check: a registered model must resolve in the price map.
            sample_key = next(iter(raw))
            if sample_key not in litellm.model_cost:
                logger.warning(
                    "Catalog sample model %s missing from litellm.model_cost "
                    "after registration",
                    sample_key,
                )
        except Exception:  # noqa: BLE001 - litellm.register_model may reject bad pricing
            logger.exception("Failed to register model price catalog with litellm")
            return False

        _loaded = True
        _metadata = metadata if isinstance(metadata, dict) else None
        logger.info(
            "Loaded model price catalog: %s models (fetched_at=%s)",
            len(raw),
            (_metadata or {}).get("fetched_at"),
        )
        return True


def catalog_metadata() -> Optional[Dict[str, Any]]:
    """Return snapshot provenance (source_url, fetched_at, model_count).

    Reads the file lazily when the catalog has not been loaded yet so
    API responses can report staleness without forcing registration.
    """
    global _metadata
    with _lock:
        if _metadata is None:
            try:
                raw = json.loads(CATALOG_PATH.read_text())
                meta = raw.get(META_KEY)
                _metadata = meta if isinstance(meta, dict) else None
            except (OSError, ValueError):
                return None
        return _metadata


# ---------------------------------------------------------------------------
# Live lookup for models missing from the snapshot
# ---------------------------------------------------------------------------


def _download_remote_price_map() -> Optional[Dict[str, Any]]:
    """Download litellm's published price map.

    When ``MODEL_PRICE_MAP_SHA256`` is set the response body must match that
    digest or the fetch is rejected, so a compromised upstream revision cannot
    silently change pricing.

    Returns:
        The decoded map, or None on any failure (the caller starts the
        failure backoff).
    """
    try:
        import httpx
    except ImportError:
        # Optional dependency for live lookups; do not retry a missing module.
        logger.warning("httpx is unavailable; skipping live model price lookup")
        return None

    attempts = max(1, _REMOTE_FETCH_RETRIES + 1)
    for attempt in range(attempts):
        try:
            response = httpx.get(REMOTE_PRICE_MAP_URL, timeout=30.0)
            response.raise_for_status()
            body = response.content
            if REMOTE_PRICE_MAP_SHA256:
                digest = hashlib.sha256(body).hexdigest()
                if digest != REMOTE_PRICE_MAP_SHA256:
                    raise ValueError(
                        "Remote price map SHA-256 mismatch "
                        f"(expected {REMOTE_PRICE_MAP_SHA256}, got {digest})"
                    )
            fetched = response.json()
            return fetched if isinstance(fetched, dict) else None
        except Exception:  # noqa: BLE001 - network is best-effort here
            logger.warning(
                "Live model price map download failed (attempt %s/%s)",
                attempt + 1,
                attempts,
                exc_info=True,
            )
    return None


def _fetch_remote_price_map() -> Optional[Dict[str, Any]]:
    """Return litellm's price map, honoring cache and failure backoff.

    Returns:
        The cached/downloaded map, or None while a failure backoff is in
        effect or the download fails.
    """
    return _remote_cache.get(_download_remote_price_map)


def _positive_price(value: Any) -> Optional[float]:
    """Parse a vendor price string into a positive float.

    Args:
        value: Raw price value from the vendor payload.

    Returns:
        The parsed price, or None when absent, unparseable or not positive.
        Zero is rejected so a missing price is never asserted as free.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _openrouter_entries_from_payload(payload: Any) -> Dict[str, Any]:
    """Convert an OpenRouter ``/models`` payload into litellm price entries.

    OpenRouter reports prices in USD per token as decimal strings, which is
    already litellm's unit, so values are carried across without rescaling.
    Models without a usable positive prompt/completion price are skipped so
    they stay honestly unpriced instead of being recorded as free.

    Args:
        payload: Decoded JSON body from the OpenRouter models endpoint.

    Returns:
        Mapping of ``openrouter/<model id>`` to litellm-shaped price entries.
    """
    if isinstance(payload, dict):
        models = payload.get("data")
    else:
        models = payload
    if not isinstance(models, list):
        return {}

    entries: Dict[str, Any] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        pricing = model.get("pricing")
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        prompt_cost = _positive_price(pricing.get("prompt"))
        completion_cost = _positive_price(pricing.get("completion"))
        if prompt_cost is None and completion_cost is None:
            continue

        entry: Dict[str, Any] = {
            "litellm_provider": "openrouter",
            "mode": "chat",
            "input_cost_per_token": prompt_cost or 0.0,
            "output_cost_per_token": completion_cost or 0.0,
        }
        cache_read = _positive_price(pricing.get("input_cache_read"))
        if cache_read is not None:
            entry["cache_read_input_token_cost"] = cache_read
        cache_write = _positive_price(pricing.get("input_cache_write"))
        if cache_write is not None:
            entry["cache_creation_input_token_cost"] = cache_write
        entries[f"{_OPENROUTER_PREFIX}{model_id.strip()}"] = entry

    return entries


def _download_openrouter_price_map() -> Optional[Dict[str, Any]]:
    """Download OpenRouter's model list and convert it to price entries.

    Returns:
        Mapping of ``openrouter/<model id>`` to price entries, or None on any
        failure or when the payload yields no usable prices (the caller starts
        the failure backoff).
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx is unavailable; skipping OpenRouter price lookup")
        return None

    try:
        response = httpx.get(OPENROUTER_MODELS_URL, timeout=30.0)
        response.raise_for_status()
        entries = _openrouter_entries_from_payload(response.json())
    except Exception:  # noqa: BLE001 - network is best-effort here
        logger.warning("OpenRouter price list download failed", exc_info=True)
        return None

    return entries or None


def _fetch_openrouter_price_map() -> Optional[Dict[str, Any]]:
    """Return OpenRouter's price map, with cache and failure backoff.

    Returns:
        Mapping of ``openrouter/<model id>`` to price entries, or None when a
        backoff is in effect or the download fails.
    """
    return _openrouter_cache.get(_download_openrouter_price_map)


def lookup_model_price_now(candidates: List[str]) -> Optional[str]:
    """Try to price the given model candidates from the live upstream map.

    Registers the first matching entry with litellm so subsequent requests
    price locally. Candidates with no upstream entry enter the negative cache.

    Args:
        candidates: Model-name candidates, most specific first (typically from
            ``model_pricing._iter_litellm_model_candidates``).

    Returns:
        The matched upstream key, or None when nothing matched.
    """
    now = time.monotonic()
    with _lookup_lock:
        fresh = [
            candidate
            for candidate in candidates
            if now - _negative_cache.get(candidate, -_NEGATIVE_TTL_SECONDS)
            >= _NEGATIVE_TTL_SECONDS
        ]
    if not fresh:
        return None

    primary = _fetch_remote_price_map()
    # Marketplace-routed models (openrouter/vendor/model) are frequently
    # absent from litellm's map; OpenRouter itself is authoritative for them.
    marketplace: Optional[Dict[str, Any]] = None
    if any(candidate.startswith(_OPENROUTER_PREFIX) for candidate in fresh):
        marketplace = _fetch_openrouter_price_map()
    if primary is None and marketplace is None:
        # Every source is unavailable: retry later rather than negative-cache
        # a model that may well be priceable.
        return None
    remote: Dict[str, Any] = {**(marketplace or {}), **(primary or {})}

    matched_key: Optional[str] = None
    for candidate in fresh:
        entry = remote.get(candidate)
        if isinstance(entry, dict) and (
            entry.get("input_cost_per_token") is not None
            or entry.get("output_cost_per_token") is not None
        ):
            try:
                import litellm

                litellm.register_model({candidate: entry})
                matched_key = candidate
                logger.info(
                    "Live price lookup registered %s from upstream map",
                    _model_log_token(candidate),
                )
                break
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to register live price for %s",
                    _model_log_token(candidate),
                )
                return None

    with _lookup_lock:
        stamp = time.monotonic()
        for candidate in fresh:
            if candidate != matched_key:
                _negative_cache[candidate] = stamp
    return matched_key


def schedule_price_lookup(*, ai_model: Any, api_usage_id: Optional[str] = None) -> bool:
    """Schedule a one-shot background price lookup for an unpriced model.

    Fired from the gateway recording path when a usage row lands as
    ``unpriced``. Runs off the hot path via a bounded thread pool; when the
    lookup succeeds and ``api_usage_id`` is given, the triggering row is
    re-priced in place. De-duplicated against in-flight lookups and the
    negative cache.

    Args:
        ai_model: The AIModel the request was routed to.
        api_usage_id: The unpriced ``ApiUsage`` row to fix on success.

    Returns:
        True when a lookup was submitted to the pool.
    """
    import os

    from preloop.config import settings

    if not getattr(settings, "model_price_live_lookup_enabled", True):
        return False
    if os.getenv("TESTING") == "true":
        return False

    from preloop.services.model_pricing import _iter_litellm_model_candidates

    candidates = list(_iter_litellm_model_candidates(ai_model))
    if not candidates:
        return False

    dedupe_key = candidates[0]
    log_token = _model_log_token(dedupe_key)
    now = time.monotonic()
    with _lookup_lock:
        if dedupe_key in _pending_lookups:
            return False
        if all(
            now - _negative_cache.get(candidate, -_NEGATIVE_TTL_SECONDS)
            < _NEGATIVE_TTL_SECONDS
            for candidate in candidates
        ):
            return False
        _pending_lookups.add(dedupe_key)

    def _run() -> None:
        try:
            matched = lookup_model_price_now(candidates)
            if matched and api_usage_id:
                _reprice_usage_row(api_usage_id)
        except Exception:  # noqa: BLE001 - background best-effort
            logger.exception("Live price lookup failed for %s", log_token)
        finally:
            with _lookup_lock:
                _pending_lookups.discard(dedupe_key)

    _LOOKUP_EXECUTOR.submit(_run)
    return True


def _reprice_usage_row(api_usage_id: str) -> None:
    """Re-price one usage row after a successful live lookup."""
    from preloop.models.db.session import get_db_session
    from preloop.services.usage_repricing import reprice_single_row

    db = next(get_db_session())
    try:
        reprice_single_row(db, api_usage_id=api_usage_id)
    finally:
        db.close()


def reset_lookup_state_for_tests() -> None:
    """Clear all live-lookup caches (test isolation only)."""
    _remote_cache.reset()
    _openrouter_cache.reset()
    with _lookup_lock:
        _negative_cache.clear()
        _pending_lookups.clear()


def _module_state_for_tests() -> dict[str, Any]:
    """Expose module cache state for tests and diagnostics."""
    return {
        "loaded": _loaded,
        "remote": _remote_cache.snapshot(),
        "openrouter": _openrouter_cache.snapshot(),
    }
