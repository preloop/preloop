"""Vendored model-price catalog loader and live price lookup.

Preloop vendors a filtered snapshot of litellm's public price map at
``services/data/model_prices.json`` (regenerated with
``scripts/update_model_prices.py``). Loading it at startup makes default
pricing deterministic per release instead of depending on whichever litellm
version happens to be installed.

The snapshot only changes on deploy, so every serving process also refreshes
the upstream map in the background (:class:`PriceMapRefresher`): once on
startup and again every ``_REMOTE_TTL_SECONDS``, merging new and changed
upstream prices over the snapshot. Operator-reviewed prices (the reviewed
feed's ``preloop_price_provenance`` entries) and the snapshot's first-party
overlays (``moonshot/``, ``zai/``) are never overwritten by upstream.

The on-miss lookup below stays as the fallback for anything the eager refresh
has not priced yet. When the gateway records an ``unpriced``
usage row, :func:`schedule_price_lookup` is given the model id (never a
credential-bearing snapshot), re-reads the row through CRUD on a worker
Session, fetches the model's price from the live upstream map ONCE via a
bounded background pool, registers it with litellm, and re-prices the
triggering row. Lookups are throttled hard:

- the downloaded upstream map is cached in-process for ``_REMOTE_TTL_SECONDS``,
- failed downloads back off for ``_REMOTE_FAILURE_BACKOFF_SECONDS``,
- model names that the upstream map does not contain enter a negative cache
  for ``_NEGATIVE_TTL_SECONDS`` so the same unknown model never triggers
  repeated work.

Every fetch logs exactly one line (INFO on success, WARNING on failure) and
every negative-cache insertion logs one WARNING, so a miss can be diagnosed
from the logs after the fact. :func:`price_map_status` feeds the health
endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional

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

# First-party overlay rows in the vendored snapshot are sourced from vendor
# pages, not litellm (see scripts/update_model_prices.py), so the runtime
# merge must not replace them with litellm's numbers.
_OVERLAY_KEY_PREFIXES = ("moonshot/", "zai/")
_OVERLAY_PROVIDERS = frozenset({"moonshot", "zai"})

_lock = threading.Lock()
_loaded = False
_metadata: Optional[Dict[str, Any]] = None
_vendored_overlay_keys: FrozenSet[str] = frozenset()


def _monotonic() -> float:
    """Return the monotonic clock every cache window is measured against.

    A module-level seam so tests can drive TTL, backoff and negative-cache
    windows with a fake clock instead of sleeping.
    """
    return time.monotonic()


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
    # Bumped on every successful download so the eager refresher can tell a
    # new map from the one it already merged (timestamps can repeat).
    _generation: int = field(default=0, repr=False)

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
        now = _monotonic()
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
            self._fetched_at = _monotonic()
            self._failed_at = 0.0
            self._generation += 1

    def mark_failed(self) -> None:
        """Start the failure backoff window after a failed download."""
        with _lookup_lock:
            self._failed_at = _monotonic()

    def reset(self) -> None:
        """Drop the cached map and any backoff state (test isolation only)."""
        with _lookup_lock:
            self._map = None
            self._fetched_at = 0.0
            self._failed_at = 0.0
            self._generation = 0

    def seconds_until_due(self) -> float:
        """Return how long until ``get`` would download again (0 when due).

        Used by the eager refresher to sleep exactly until the cached map
        goes stale or the failure backoff ends, whichever applies.
        """
        now = _monotonic()
        with _lookup_lock:
            if self._failed_at and now - self._failed_at < (
                _REMOTE_FAILURE_BACKOFF_SECONDS
            ):
                return _REMOTE_FAILURE_BACKOFF_SECONDS - (now - self._failed_at)
            if self._map is not None and now - self._fetched_at < _REMOTE_TTL_SECONDS:
                return _REMOTE_TTL_SECONDS - (now - self._fetched_at)
            return 0.0

    def snapshot(self) -> Dict[str, Any]:
        """Return the cache's current state for tests and diagnostics."""
        with _lookup_lock:
            return {
                "map": self._map,
                "fetched_at": self._fetched_at,
                "failed_at": self._failed_at,
                "generation": self._generation,
            }


# litellm's published map; the primary source for every model.
_remote_cache = _PriceMapCache("litellm")
# OpenRouter's own model list; secondary source for marketplace-routed models.
_openrouter_cache = _PriceMapCache("openrouter")

# candidate name -> monotonic time of the failed lookup (negative cache).
_negative_cache: Dict[str, float] = {}
_MAX_NEGATIVE_CACHE_ENTRIES = 4096
# candidate names with a lookup currently in flight.
_pending_lookups: set[str] = set()
_pending_usage: dict[str, list[tuple[str, bool]]] = {}
_MAX_PENDING_USAGE_PER_LOOKUP = 500
# Bound concurrent live lookups so unknown models cannot spawn unbounded threads.
_LOOKUP_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="price-lookup")


def _model_log_token(model_name: str) -> str:
    """Return a stable log token that never embeds the raw model name."""
    digest = hashlib.sha256(model_name.encode("utf-8", errors="replace")).hexdigest()
    return f"model#{digest[:12]}"


def _log_namespace(key: str) -> str:
    """Return the public price-map namespace of a key, for log lines.

    Namespaces (``gemini``, ``openrouter``, ``alibaba``) come from Preloop's
    own provider mapping, so naming them leaks nothing; the model part of the
    key stays hashed by :func:`_model_log_token`.
    """
    head, separator, _ = key.partition("/")
    if not separator:
        head, separator, _ = key.partition(":")
    return head.lower() if separator and head.isascii() and len(head) <= 32 else "-"


def _remember_negative_lookup(
    key: str, stamp: Optional[float] = None, *, reason: Optional[str] = None
) -> None:
    """Record a failed lookup. Caller must hold ``_lookup_lock``.

    A key that is already negative-cached keeps its original stamp, so the
    window cannot be extended by overlapping lookups and the WARNING below
    fires once per ``_NEGATIVE_TTL_SECONDS`` window per alias.

    Args:
        key: Candidate alias (or native catalog dedupe key) that missed.
        stamp: Monotonic time of the miss; defaults to now.
        reason: Why the key is being cached. When given, the insertion is
            logged as a WARNING naming the alias by namespace and log token
            (the raw model name is never logged).
    """
    now = stamp if stamp is not None else _monotonic()
    cached_at = _negative_cache.get(key)
    if cached_at is not None and now - cached_at < _NEGATIVE_TTL_SECONDS:
        return
    if len(_negative_cache) >= _MAX_NEGATIVE_CACHE_ENTRIES:
        expired = [
            cached_key
            for cached_key, cached_at in _negative_cache.items()
            if now - cached_at >= _NEGATIVE_TTL_SECONDS
        ]
        for cached_key in expired:
            _negative_cache.pop(cached_key, None)
        while len(_negative_cache) >= _MAX_NEGATIVE_CACHE_ENTRIES:
            oldest = min(_negative_cache, key=_negative_cache.get)  # type: ignore[arg-type]
            _negative_cache.pop(oldest, None)
    _negative_cache[key] = now
    if reason is not None:
        logger.warning(
            "Model price negative-cached: alias=%s namespace=%s reason=%s "
            "ttl_seconds=%s",
            _model_log_token(key),
            _log_namespace(key),
            reason,
            _NEGATIVE_TTL_SECONDS,
        )


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
    global _loaded, _metadata, _vendored_overlay_keys
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
        _vendored_overlay_keys = frozenset(
            key
            for key, entry in raw.items()
            if key.startswith(_OVERLAY_KEY_PREFIXES)
            or (
                isinstance(entry, dict)
                and entry.get("litellm_provider") in _OVERLAY_PROVIDERS
            )
        )
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


# Last-fetch bookkeeping for the litellm map, surfaced on the health
# endpoint. Wall-clock times (not monotonic) because operators read them.
_price_map_status: Dict[str, Any] = {
    "source": "litellm",
    "last_attempt_at": None,
    "last_success_at": None,
    "last_outcome": None,
    "last_failure_reason": None,
    "entry_count": None,
    "registered_count": None,
    "sha256": None,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_fetch(**fields: Any) -> None:
    """Merge fields into the fetch status (thread-safe)."""
    with _lookup_lock:
        _price_map_status.update(fields)


def _fetch_failure_reason(exc: BaseException) -> str:
    """Return a short, secret-free reason for a failed download.

    The configured URL and the response body are never included: an operator
    URL can carry an access token in its query string.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return f"http_{status_code}"
    if isinstance(exc, ValueError) and "SHA-256" in str(exc):
        return "sha256_mismatch"
    return type(exc).__name__


def _download_remote_price_map() -> Optional[Dict[str, Any]]:
    """Download litellm's published price map.

    When ``MODEL_PRICE_MAP_SHA256`` is set the response body must match that
    digest or the fetch is rejected, so a compromised upstream revision cannot
    silently change pricing.

    Logs exactly one line per call, whatever the number of attempts: INFO
    with the entry count and body digest on success, WARNING with the reason
    on failure. Individual attempts log at DEBUG.

    Returns:
        The decoded map, or None on any failure (the caller starts the
        failure backoff).
    """
    started_at = _utc_now_iso()
    try:
        import httpx
    except ImportError:
        # Optional dependency for live lookups; do not retry a missing module.
        _record_fetch(
            last_attempt_at=started_at,
            last_outcome="failed",
            last_failure_reason="httpx_unavailable",
        )
        logger.warning(
            "Model price map fetch: source=litellm outcome=failed "
            "reason=httpx_unavailable attempts=0"
        )
        return None

    attempts = max(1, _REMOTE_FETCH_RETRIES + 1)
    reason = "unknown"
    for attempt in range(attempts):
        try:
            response = httpx.get(REMOTE_PRICE_MAP_URL, timeout=30.0)
            response.raise_for_status()
            body = response.content
            digest = hashlib.sha256(body).hexdigest()
            if REMOTE_PRICE_MAP_SHA256 and digest != REMOTE_PRICE_MAP_SHA256:
                raise ValueError(
                    "Remote price map SHA-256 mismatch "
                    f"(expected {REMOTE_PRICE_MAP_SHA256}, got {digest})"
                )
            fetched = response.json()
            if not isinstance(fetched, dict) or not fetched:
                # An empty map would negative-cache every model for a day.
                raise ValueError("Remote price map is not a non-empty JSON object")
        except Exception as exc:  # noqa: BLE001 - network is best-effort here
            reason = _fetch_failure_reason(exc)
            logger.debug(
                "Model price map download attempt %s/%s failed: %s",
                attempt + 1,
                attempts,
                reason,
                exc_info=True,
            )
            continue
        _record_fetch(
            last_attempt_at=started_at,
            last_success_at=_utc_now_iso(),
            last_outcome="ok",
            last_failure_reason=None,
            entry_count=len(fetched),
            sha256=digest,
        )
        logger.info(
            "Model price map fetch: source=litellm outcome=ok entries=%s "
            "attempts=%s sha256=%s pinned=%s",
            len(fetched),
            attempt + 1,
            digest,
            bool(REMOTE_PRICE_MAP_SHA256),
        )
        return fetched

    _record_fetch(
        last_attempt_at=started_at, last_outcome="failed", last_failure_reason=reason
    )
    logger.warning(
        "Model price map fetch: source=litellm outcome=failed reason=%s "
        "attempts=%s retry_in_seconds=%s",
        reason,
        attempts,
        _REMOTE_FAILURE_BACKOFF_SECONDS,
    )
    return None


def _fetch_remote_price_map() -> Optional[Dict[str, Any]]:
    """Return litellm's price map, honoring cache and failure backoff.

    Returns:
        The cached/downloaded map, or None while a failure backoff is in
        effect or the download fails.
    """
    return _remote_cache.get(_download_remote_price_map)


def price_map_status() -> Dict[str, Any]:
    """Return the last upstream fetch for the health/diagnostics endpoint.

    Contains no URL and no model names: ``last_attempt_at`` and
    ``last_success_at`` (UTC ISO-8601), ``last_outcome`` (``ok`` or
    ``failed``), ``last_failure_reason``, ``entry_count`` (entries in the
    last good map), ``registered_count`` (entries the last merge added or
    changed), ``sha256`` of the last good body, and the snapshot's own
    ``snapshot_fetched_at``.
    """
    with _lookup_lock:
        status = dict(_price_map_status)
    meta = catalog_metadata() or {}
    status["snapshot_fetched_at"] = meta.get("fetched_at")
    status["refresh_interval_seconds"] = _REMOTE_TTL_SECONDS
    return status


# ---------------------------------------------------------------------------
# Eager refresh: merge the upstream map over the snapshot
# ---------------------------------------------------------------------------

# Non-cost fields carried from an upstream row. Mirrors KEEP_FIELDS_EXACT in
# scripts/update_model_prices.py (deprecation_date aside: a deprecated model
# that still receives traffic should stay priced). register_model merges
# per key, so litellm's bundled capability flags survive.
_MERGE_METADATA_FIELDS = frozenset(
    {"litellm_provider", "mode", "max_tokens", "max_input_tokens", "max_output_tokens"}
)
# LiteLLM memoizes model info; a changed price must not be served stale.
_LITELLM_MODEL_INFO_CACHES = (
    "get_model_info",
    "_cached_get_model_info",
    "_cached_get_model_info_helper",
)
_merged_generation = 0


def _flatten_tiered_prices(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Lift the lowest ``tiered_pricing`` tier onto missing top-level prices.

    Same rule as ``_flatten_tiered_pricing`` in
    ``scripts/update_model_prices.py`` (a parity test keeps them aligned):
    a flat price the row publishes wins, a missing or null field takes the
    lowest tier's value, so a tier-only row is not merged priceless.
    """
    tiers = entry.get("tiered_pricing")
    if not isinstance(tiers, list):
        return entry
    candidates = [tier for tier in tiers if isinstance(tier, dict)]
    if not candidates:
        return entry

    def tier_start(tier: Dict[str, Any]) -> float:
        bounds = tier.get("range")
        if isinstance(bounds, list) and bounds and isinstance(bounds[0], (int, float)):
            return float(bounds[0])
        return 0.0

    lowest = min(candidates, key=tier_start)
    merged = dict(entry)
    for name, value in lowest.items():
        if "cost" in name and isinstance(value, (int, float)):
            if merged.get(name) is None:
                merged[name] = value
    return merged


def _normalize_upstream_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Reduce one upstream row to the fields pricing needs, or None.

    Null fields are dropped so an upstream ``null`` never erases a price the
    snapshot holds. Rows without a numeric input or output price are
    skipped (they would only make the model look priced), as are embedding
    rows without a per-token input price, which the token ledger could only
    bill as $0.
    """
    if not isinstance(entry, dict):
        return None
    flattened = _flatten_tiered_prices(entry)
    normalized = {
        name: value
        for name, value in flattened.items()
        if value is not None and (name in _MERGE_METADATA_FIELDS or "cost" in name)
    }
    input_cost = normalized.get("input_cost_per_token")
    output_cost = normalized.get("output_cost_per_token")
    has_input = isinstance(input_cost, (int, float))
    if not has_input and not isinstance(output_cost, (int, float)):
        return None
    if normalized.get("mode") == "embedding" and not has_input:
        return None
    return normalized


def _is_protected_price(key: str, current: Any) -> bool:
    """True when the key's current price outranks upstream.

    Operator-reviewed prices (applied by the reviewed feed, which stamps
    ``preloop_price_provenance``/``preloop_price_policy``) and first-party
    overlay rows from the snapshot are Preloop decisions, not litellm data.
    """
    if key in _vendored_overlay_keys:
        return True
    return isinstance(current, dict) and bool(
        current.get("preloop_price_provenance") or current.get("preloop_price_policy")
    )


def _clear_litellm_model_info_caches() -> None:
    """Best-effort clear of LiteLLM's memoized model info after a price change."""
    try:
        from litellm import utils as litellm_utils
    except ImportError:  # pragma: no cover - litellm is a core dependency
        return
    for name in _LITELLM_MODEL_INFO_CACHES:
        clear = getattr(getattr(litellm_utils, name, None), "cache_clear", None)
        if callable(clear):
            with suppress(Exception):
                clear()


def merge_upstream_prices(price_map: Dict[str, Any]) -> int:
    """Register new and changed upstream prices with litellm.

    Only rows that are missing from ``litellm.model_cost`` or whose cost
    fields differ are registered, so a steady-state refresh is a cheap diff
    rather than thousands of ``register_model`` calls. Holds the catalog
    lock so it serializes with the snapshot load and the reviewed feed.

    Args:
        price_map: Decoded upstream map (litellm's
            ``model_prices_and_context_window.json`` shape).

    Returns:
        Number of entries registered.
    """
    import litellm

    with _lock:
        changes: Dict[str, Dict[str, Any]] = {}
        replaced_existing = False
        for key, raw_entry in price_map.items():
            if not isinstance(key, str) or key == "sample_spec":
                continue
            entry = _normalize_upstream_entry(raw_entry)
            if entry is None:
                continue
            current = litellm.model_cost.get(key)
            if _is_protected_price(key, current):
                continue
            if isinstance(current, dict) and all(
                current.get(name) == value
                for name, value in entry.items()
                if "cost" in name
            ):
                continue
            replaced_existing = replaced_existing or current is not None
            changes[key] = entry
        if changes:
            litellm.register_model(changes)
            if replaced_existing:
                _clear_litellm_model_info_caches()
    return len(changes)


def refresh_price_map_once() -> float:
    """Run one eager refresh cycle and return seconds until the next one.

    Downloads the upstream map when the shared cache is stale (the on-miss
    path uses the same cache, so a map it already fetched is merged without
    a second download), merges it over the snapshot, and respects the
    failure backoff. Never raises.
    """
    global _merged_generation
    price_map = _fetch_remote_price_map()
    generation = _remote_cache.snapshot()["generation"]
    if price_map is not None and generation != _merged_generation:
        try:
            registered = merge_upstream_prices(price_map)
        except Exception:  # noqa: BLE001 - keep serving the snapshot
            logger.exception("Merging the upstream model price map failed")
        else:
            _merged_generation = generation
            _record_fetch(registered_count=registered)
            logger.info(
                "Model price map merged: source=litellm registered=%s", registered
            )
    return _remote_cache.seconds_until_due()


class PriceMapRefresher:
    """Lifecycle-owned task that keeps the upstream price map merged.

    Runs :func:`refresh_price_map_once` in a worker thread (download and
    ``register_model`` are blocking) on start and then whenever the cache is
    due: every ``_REMOTE_TTL_SECONDS`` after a success, or once the failure
    backoff has elapsed after a failure.
    """

    # Lower bound between cycles so a zero delay cannot spin the loop.
    MIN_DELAY_SECONDS = 1.0

    def __init__(self) -> None:
        self.task: Optional[asyncio.Task[None]] = None
        self.first_cycle_done = asyncio.Event()

    async def run(self) -> None:
        """Refresh until cancelled."""
        while True:
            try:
                delay = await asyncio.to_thread(refresh_price_map_once)
            except Exception:  # noqa: BLE001 - never let the loop die
                logger.exception("Model price map refresh cycle failed")
                delay = float(_REMOTE_FAILURE_BACKOFF_SECONDS)
            self.first_cycle_done.set()
            await asyncio.sleep(max(self.MIN_DELAY_SECONDS, delay))

    def start(self) -> None:
        """Start at most one task for this lifecycle owner."""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="model-price-map-refresh")

    async def stop(self) -> None:
        """Cancel the task and wait for it to finish."""
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None


def start_price_map_refresh() -> Optional[PriceMapRefresher]:
    """Start the eager refresh unless live lookups are off or under tests.

    Gated by ``model_price_live_lookup_enabled`` (the same switch as the
    on-miss lookup) so an air-gapped deployment turns off every upstream
    price fetch with one setting. Must be called from a running event loop.
    """
    from preloop.config import settings

    if os.getenv("TESTING") == "true" or not getattr(
        settings, "model_price_live_lookup_enabled", True
    ):
        return None
    refresher = PriceMapRefresher()
    refresher.start()
    return refresher


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
        logger.warning(
            "Model price map fetch: source=openrouter outcome=failed "
            "reason=httpx_unavailable"
        )
        return None

    try:
        response = httpx.get(OPENROUTER_MODELS_URL, timeout=30.0)
        response.raise_for_status()
        entries = _openrouter_entries_from_payload(response.json())
    except Exception as exc:  # noqa: BLE001 - network is best-effort here
        logger.debug("OpenRouter price list download failed", exc_info=True)
        logger.warning(
            "Model price map fetch: source=openrouter outcome=failed reason=%s "
            "retry_in_seconds=%s",
            _fetch_failure_reason(exc),
            _REMOTE_FAILURE_BACKOFF_SECONDS,
        )
        return None

    if not entries:
        logger.warning(
            "Model price map fetch: source=openrouter outcome=failed "
            "reason=no_priced_models retry_in_seconds=%s",
            _REMOTE_FAILURE_BACKOFF_SECONDS,
        )
        return None
    logger.info(
        "Model price map fetch: source=openrouter outcome=ok entries=%s",
        len(entries),
    )
    return entries


def _fetch_openrouter_price_map() -> Optional[Dict[str, Any]]:
    """Return OpenRouter's price map, with cache and failure backoff.

    Returns:
        Mapping of ``openrouter/<model id>`` to price entries, or None when a
        backoff is in effect or the download fails.
    """
    return _openrouter_cache.get(_download_openrouter_price_map)


def fetch_openrouter_price_map() -> Optional[Dict[str, Any]]:
    """Return OpenRouter's published prices, keyed ``openrouter/<model id>``.

    The public read used by the model page's "Fetch from provider": it shares
    the same cache and failure backoff as the gateway's own lookups, so a
    button nobody stops pressing cannot turn into a stream of upstream calls.

    Returns:
        The price map, or None when the download failed or a backoff is on.
    """
    return _fetch_openrouter_price_map()


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
    now = _monotonic()
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

                with _lock:
                    existing = litellm.model_cost.get(candidate, {})
                    if not existing.get("preloop_price_provenance"):
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
        stamp = _monotonic()
        for candidate in fresh:
            if candidate != matched_key:
                _remember_negative_lookup(
                    candidate,
                    stamp,
                    # A sibling alias matched: this is bookkeeping, not a miss.
                    reason=None if matched_key else "not_in_map",
                )
    return matched_key


@contextmanager
def _ai_model_price_lookup_session(ai_model_id: Any) -> Iterator[Any]:
    """Yield the AIModel row on a worker-owned Session.

    HTTP accounting passes only the model id so snapshots never carry
    credentials into the lookup pool. Resolve ``credentials_secret`` and
    prepare any native request inside this context; perform network I/O only
    after it closes.
    """
    from preloop.models.crud import crud_ai_model
    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        yield crud_ai_model.get(db, id=ai_model_id)
    finally:
        db.close()


def schedule_price_lookup(
    *,
    ai_model_id: Any,
    api_usage_id: Optional[str] = None,
    notify_after_lookup: bool = False,
) -> bool:
    """Schedule bounded recovery, repairing queued rows before unresolved alerts.

    True means recovery owns this row, including when joined to an in-flight
    lookup. False means disabled, unavailable, negative-cached, or queue full;
    callers retain responsibility for an immediate unresolved alert.
    Only identifiers enter the pool. Credentials are resolved on the worker,
    and its database session closes before provider I/O.
    """
    from preloop.config import settings
    from preloop.services import alibaba_pricing
    from preloop.services.alibaba_price_catalog import native_catalog_target, region_key
    from preloop.services.model_pricing import _iter_litellm_model_candidates
    from preloop.services.litellm_routing import endpoint_host

    if (
        not getattr(settings, "model_price_live_lookup_enabled", True)
        or os.getenv("TESTING") == "true"
        or ai_model_id is None
    ):
        return False
    with _ai_model_price_lookup_session(ai_model_id) as ai_model:
        if ai_model is None:
            return False
        is_alibaba_model = alibaba_pricing.is_alibaba(ai_model)
        target = native_catalog_target(ai_model) if is_alibaba_model else None
        identifier = (ai_model.model_identifier or "").strip() or "unknown"
        credential_host = (
            endpoint_host(getattr(ai_model, "api_endpoint", None)) or "default"
        )
        candidates = (
            [] if is_alibaba_model else list(_iter_litellm_model_candidates(ai_model))
        )
    if is_alibaba_model:
        if target is None:
            return False
        dedupe_key = f"alibaba:{region_key(target[1])}:{credential_host}:{identifier}"
        negative_keys = [dedupe_key]
    else:
        if not candidates:
            return False
        dedupe_key = candidates[0]
        negative_keys = candidates
    now = _monotonic()
    with _lookup_lock:
        if dedupe_key in _pending_lookups:
            queued = _pending_usage.setdefault(dedupe_key, [])
            if not api_usage_id or len(queued) >= _MAX_PENDING_USAGE_PER_LOOKUP:
                return False
            queued.append((api_usage_id, notify_after_lookup))
            return True
        if all(
            now - _negative_cache.get(key, -_NEGATIVE_TTL_SECONDS)
            < _NEGATIVE_TTL_SECONDS
            for key in negative_keys
        ):
            return False
        _pending_lookups.add(dedupe_key)
        _pending_usage[dedupe_key] = (
            [(api_usage_id, notify_after_lookup)] if api_usage_id else []
        )

    def _run() -> None:
        status = "failed"
        matched = False
        try:
            if is_alibaba_model:
                from preloop.services.alibaba_price_catalog import (
                    CatalogRefreshStatus,
                    prepare_refresh,
                    refresh_prepared,
                )

                with _ai_model_price_lookup_session(ai_model_id) as live_model:
                    prepared = (
                        prepare_refresh(live_model)
                        if live_model is not None
                        else CatalogRefreshStatus.no_target
                    )
                outcome = (
                    prepared
                    if isinstance(prepared, CatalogRefreshStatus)
                    else refresh_prepared(prepared)
                )
                status = outcome.value
                matched = outcome is CatalogRefreshStatus.ingested
                # A successful download may still lack this SKU or its cache
                # rate. Repeating the same regional download on every next
                # unpriced request cannot repair that billing dimension.
                with _lookup_lock:
                    _remember_negative_lookup(
                        dedupe_key,
                        reason=None if matched else f"native_catalog_{status}",
                    )
            else:
                matched = bool(lookup_model_price_now(candidates))
                status = "ingested" if matched else "unavailable"
        except Exception:  # noqa: BLE001 - background best-effort
            logger.exception(
                "Live price lookup failed for %s", _model_log_token(dedupe_key)
            )
        finally:
            with _lookup_lock:
                queued = _pending_usage.pop(dedupe_key, [])
                _pending_lookups.discard(dedupe_key)
            for usage_id, notify in queued:
                row_status = status
                try:
                    if matched:
                        _reprice_usage_row(usage_id)
                except Exception:  # noqa: BLE001 - preserve unresolved notification
                    row_status = f"{status}_repricing_failed"
                    logger.exception("Post-refresh usage recovery failed")
                if notify:
                    try:
                        from preloop.services.unpriced_model_alert import (
                            notify_unpriced_usage_row,
                        )

                        notify_unpriced_usage_row(usage_id, refresh_status=row_status)
                    except Exception:  # noqa: BLE001 - one row must not block others
                        logger.exception("Post-refresh unresolved notification failed")

    try:
        _LOOKUP_EXECUTOR.submit(_run)
    except Exception:
        with _lookup_lock:
            queued = _pending_usage.pop(dedupe_key, [])
            _pending_lookups.discard(dedupe_key)
        for usage_id, notify in queued:
            if not notify:
                continue
            try:
                from preloop.services.unpriced_model_alert import (
                    notify_unpriced_usage_row,
                )

                notify_unpriced_usage_row(usage_id, refresh_status="submit_failed")
            except Exception:  # noqa: BLE001 - one row must not block others
                logger.exception("Submit-failure unresolved notification failed")
        raise
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
    global _merged_generation
    _remote_cache.reset()
    _openrouter_cache.reset()
    with _lookup_lock:
        _negative_cache.clear()
        _pending_lookups.clear()
        _pending_usage.clear()
        for key in _price_map_status:
            if key != "source":
                _price_map_status[key] = None
        _merged_generation = 0


def _module_state_for_tests() -> dict[str, Any]:
    """Expose module cache state for tests and diagnostics."""
    return {
        "loaded": _loaded,
        "remote": _remote_cache.snapshot(),
        "openrouter": _openrouter_cache.snapshot(),
    }
