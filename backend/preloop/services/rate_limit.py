"""Shared request rate limiter for public and auth-sensitive endpoints (EE #66).

Every rule is a fixed window: at most ``limit`` requests per ``window_s``
seconds per identity (client IP, normalized username, approval request id).

The default store is Postgres (``rate_limit_bucket``), so every API replica
draws from one budget and a restart does not reset it. ``memory`` keeps the
counters in-process; it is meant for tests and single-process development.

When the shared store fails, the rule is evaluated against the in-process
store instead and an error is logged. Authentication therefore never goes
down with the limiter, and it is never left unthrottled either: during an
outage each replica enforces the budget on its own, as before EE #66.

Configuration (environment, read on every check so operators and tests can
change it without a restart):

* ``RATE_LIMIT_ENABLED`` (default ``true``): ``false`` disables every rule.
* ``RATE_LIMIT_BACKEND`` (default ``postgres``): ``postgres`` or ``memory``.
* ``RATE_LIMIT_<RULE>`` overrides one rule as ``<limit>/<window seconds>``,
  for example ``RATE_LIMIT_AUTH_LOGIN_IP=20/60``. The rule name is upper-cased
  with dots replaced by underscores. ``0/60`` disables that rule.
* ``RATE_LIMIT_TRUSTED_PROXIES``: see :func:`client_ip_for_rate_limit`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Optional, Union

from fastapi import HTTPException, Request, WebSocket, status
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

RATE_LIMITED_CODE = "rate_limited"


@dataclass(frozen=True)
class Rule:
    """One named budget.

    Attributes:
        name: Stable rule name, used in keys, logs, metrics and env overrides.
        limit: Requests allowed per window by default.
        window_s: Window length in seconds by default.
    """

    name: str
    limit: int
    window_s: int

    @property
    def env_name(self) -> str:
        """Environment variable that overrides this rule."""
        return "RATE_LIMIT_" + self.name.upper().replace(".", "_")

    def effective(self) -> tuple[int, int]:
        """Return ``(limit, window_s)`` after applying the env override."""
        raw = os.getenv(self.env_name, "").strip()
        if not raw:
            return self.limit, self.window_s
        try:
            limit_s, _, window_s = raw.partition("/")
            limit = int(limit_s)
            window = int(window_s) if window_s else self.window_s
            if limit < 0 or window <= 0:
                raise ValueError(raw)
            return limit, window
        except ValueError:
            logger.error(
                "Ignoring invalid %s=%r; expected <limit>/<seconds>",
                self.env_name,
                raw,
            )
            return self.limit, self.window_s


# Defaults: EE #66 design proposal unless noted. Keep in sync with
# docs/guide/self-hosting/rate-limiting.md.
AUTH_LOGIN_IP = Rule("auth.login.ip", 10, 60)
# Counts failed password logins only, so a correct password is never refused
# because someone else is guessing.
AUTH_LOGIN_USER = Rule("auth.login.user", 5, 60)
AUTH_REFRESH = Rule("auth.refresh", 60, 60)
AUTH_RESET_PASSWORD = Rule("auth.reset_password", 10, 600)
AUTH_REGISTER = Rule("auth.register", 10, 3600)
OAUTH_TOKEN = Rule("oauth.token", 30, 60)
OAUTH_REGISTER = Rule("oauth.register", 10, 3600)
OAUTH_AUTHORIZE = Rule("oauth.authorize", 60, 60)
APPROVAL_PUBLIC_IP = Rule("approval.public.ip", 30, 60)
APPROVAL_PUBLIC_REQUEST = Rule("approval.public.request", 10, 60)


class RateLimitExceeded(HTTPException):
    """429 with ``Retry-After`` and the stable ``rate_limited`` code."""

    def __init__(self, rule: Rule, retry_after: int) -> None:
        self.rule = rule
        self.retry_after = max(1, int(retry_after))
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests. Try again in {self.retry_after} seconds.",
            headers={"Retry-After": str(self.retry_after)},
        )

    def body(self) -> dict[str, Any]:
        """JSON body for the app-level exception handler."""
        return {
            "detail": self.detail,
            "code": RATE_LIMITED_CODE,
            "retry_after": self.retry_after,
        }


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------

_DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
)


def _trusted_networks() -> list[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
    raw = os.getenv("RATE_LIMIT_TRUSTED_PROXIES", _DEFAULT_TRUSTED_PROXIES)
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.error("Ignoring invalid RATE_LIMIT_TRUSTED_PROXIES entry %r", part)
    return networks


def _parse_ip(
    value: str,
) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    value = value.strip().strip('"')
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]  # IPv4 with port
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def client_ip_for_rate_limit(request: Union[Request, WebSocket]) -> str:
    """Return the client address a caller cannot choose, for limiter keys.

    The chain is ``X-Forwarded-For`` entries followed by the TCP peer. It is
    walked from the right, skipping hops inside ``RATE_LIMIT_TRUSTED_PROXIES``
    (comma-separated CIDRs; default loopback and private ranges, which covers
    the Helm ingress and console nginx hops and Docker Compose). The first
    address outside those ranges is the one our own edge proxy saw, so a
    forged ``X-Forwarded-For`` prefix is never reached. ``X-Real-IP`` is
    ignored. When every hop is trusted (an all-private network), the leftmost
    parseable address is used; narrow the trusted ranges in that case.

    Args:
        request: Incoming request or WebSocket.

    Returns:
        The client address, or ``"unknown"`` when there is none.
    """
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    chain = [part for part in forwarded.split(",") if part.strip()]
    if peer:
        chain.append(peer)
    trusted = _trusted_networks()
    parsed = [ip for ip in (_parse_ip(part) for part in chain) if ip is not None]
    if not parsed:
        return peer or "unknown"
    for ip in reversed(parsed):
        if not any(ip in net for net in trusted if ip.version == net.version):
            return str(ip)
    return str(parsed[0])


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class RateLimitStoreError(Exception):
    """The shared store could not answer."""


def _window_start(now: float, window_s: int) -> float:
    return now - (now % window_s)


class MemoryStore:
    """In-process fixed windows; bounded so the map cannot grow forever."""

    MAX_KEYS = 50_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, int]] = {}

    def reset(self) -> None:
        """Drop every counter (tests, restart semantics)."""
        with self._lock:
            self._buckets.clear()

    def _prune(self, now: float) -> None:
        if len(self._buckets) < self.MAX_KEYS:
            return
        # Entries store (window_end, count); drop every finished window.
        self._buckets = {k: v for k, v in self._buckets.items() if v[0] > now}

    def hit(self, key: str, window_s: int, now: float) -> tuple[int, float]:
        """Count one request; return ``(count, window_end)``."""
        start = _window_start(now, window_s)
        end = start + window_s
        with self._lock:
            current_end, count = self._buckets.get(key, (end, 0))
            if current_end != end:
                count = 0
            count += 1
            self._prune(now)
            self._buckets[key] = (end, count)
            return count, end

    def peek(self, key: str, window_s: int, now: float) -> tuple[int, float]:
        """Return ``(count, window_end)`` without counting."""
        start = _window_start(now, window_s)
        end = start + window_s
        with self._lock:
            current_end, count = self._buckets.get(key, (end, 0))
            return (count if current_end == end else 0), end


class PostgresStore:
    """Counters in ``rate_limit_bucket``, shared by every replica."""

    PURGE_INTERVAL_S = 300

    def __init__(self, session_factory: Optional[Callable[[], Any]] = None) -> None:
        self._session_factory = session_factory
        self._last_purge = 0.0
        self._purge_lock = threading.Lock()

    def _session(self) -> Any:
        if self._session_factory is None:
            from preloop.models.db.session import get_session_factory

            self._session_factory = get_session_factory()
        return self._session_factory()

    @staticmethod
    def _dt(ts: float) -> datetime:
        return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)

    def _maybe_purge(self, db: Any, now: float) -> None:
        from preloop.models.crud import crud_rate_limit

        with self._purge_lock:
            if now - self._last_purge < self.PURGE_INTERVAL_S:
                return
            self._last_purge = now
        crud_rate_limit.purge_expired(db, now=self._dt(now))

    def hit(self, key: str, window_s: int, now: float) -> tuple[int, float]:
        """Count one request in the shared table; return ``(count, window_end)``."""
        from preloop.models.crud import crud_rate_limit

        start = _window_start(now, window_s)
        try:
            db = self._session()
            try:
                count = crud_rate_limit.hit(
                    db, key=key, window_start=self._dt(start), window_s=window_s
                )
                self._maybe_purge(db, now)
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        except Exception as exc:
            raise RateLimitStoreError(str(exc)) from exc
        return count, start + window_s

    def peek(self, key: str, window_s: int, now: float) -> tuple[int, float]:
        """Return ``(count, window_end)`` from the shared table."""
        from preloop.models.crud import crud_rate_limit

        start = _window_start(now, window_s)
        try:
            db = self._session()
            try:
                count = crud_rate_limit.peek(db, key=key, window_start=self._dt(start))
            finally:
                db.close()
        except Exception as exc:
            raise RateLimitStoreError(str(exc)) from exc
        return count, start + window_s


_memory_store = MemoryStore()
_fallback_store = MemoryStore()
_postgres_store = PostgresStore()


def reset_memory_store() -> None:
    """Clear the in-process counters (tests)."""
    _memory_store.reset()
    _fallback_store.reset()


def _primary_store() -> Union[MemoryStore, PostgresStore]:
    backend = os.getenv("RATE_LIMIT_BACKEND", "postgres").strip().lower()
    return _memory_store if backend == "memory" else _postgres_store


def set_postgres_session_factory(factory: Optional[Callable[[], Any]]) -> None:
    """Point the Postgres store at another session factory (tests)."""
    _postgres_store._session_factory = factory


def _enabled() -> bool:
    return os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

_COUNTERS: dict[str, Any] = {}
_last_store_error_log = 0.0
_STORE_ERROR_LOG_INTERVAL_S = 60.0


def _count(name: str, description: str, rule: Rule) -> None:
    counter = _COUNTERS.get(name)
    if counter is None:
        try:
            from opentelemetry import metrics

            counter = metrics.get_meter("preloop.rate_limit").create_counter(
                name, unit="{request}", description=description
            )
        except Exception:  # pragma: no cover - metrics are best effort
            logger.debug("Could not create counter %s", name, exc_info=True)
            return
        _COUNTERS[name] = counter
    try:
        counter.add(1, {"rule": rule.name})
    except Exception:  # pragma: no cover - metrics are best effort
        logger.debug("Could not record %s", name, exc_info=True)


def _record_hit(rule: Rule, key: str, retry_after: int) -> None:
    logger.warning(
        "Rate limit hit: rule=%s key=%s retry_after=%s",
        rule.name,
        key.rsplit(":", 1)[-1][:12],
        retry_after,
    )
    _count(
        "preloop_rate_limit_hits_total",
        "Requests refused with 429 by the request rate limiter",
        rule,
    )


def _record_store_error(rule: Rule, exc: Exception) -> None:
    global _last_store_error_log
    now = time.monotonic()
    if now - _last_store_error_log >= _STORE_ERROR_LOG_INTERVAL_S:
        _last_store_error_log = now
        logger.error(
            "Rate limit store unavailable (rule=%s); enforcing per process: %s",
            rule.name,
            exc,
        )
    _count(
        "preloop_rate_limit_store_errors_total",
        "Rate limit checks that fell back to the in-process store",
        rule,
    )


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def _key(rule: Rule, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{rule.name}:{digest}"


def _with_fallback(
    rule: Rule, op: str, key: str, window_s: int, now: float
) -> tuple[int, float]:
    store = _primary_store()
    try:
        if op == "peek":
            return store.peek(key, window_s, now)
        return store.hit(key, window_s, now)
    except RateLimitStoreError as exc:
        _record_store_error(rule, exc)
        if op == "peek":
            return _fallback_store.peek(key, window_s, now)
        return _fallback_store.hit(key, window_s, now)


def enforce(rule: Rule, identity: Optional[str]) -> None:
    """Count one request for ``identity`` under ``rule``; 429 when over budget.

    Blocking (the Postgres store does I/O); call from sync code or through
    :func:`enforce_async`.

    Args:
        rule: The budget to charge.
        identity: Who is charged. Empty or ``None`` skips the check, so a
            missing identity never merges every caller into one bucket.

    Raises:
        RateLimitExceeded: When this request exceeds the budget.
    """
    if not identity or not _enabled():
        return
    limit, window_s = rule.effective()
    if limit <= 0:
        return
    now = time.time()
    key = _key(rule, identity)
    count, window_end = _with_fallback(rule, "hit", key, window_s, now)
    if count > limit:
        retry_after = int(window_end - now) + 1
        _record_hit(rule, key, retry_after)
        raise RateLimitExceeded(rule, retry_after)


def check_without_counting(rule: Rule, identity: Optional[str]) -> None:
    """Raise when ``identity`` has already used the budget; count nothing.

    Pair with :func:`record` for failure-only rules such as
    ``auth.login.user``.

    Raises:
        RateLimitExceeded: When the budget is already used up.
    """
    if not identity or not _enabled():
        return
    limit, window_s = rule.effective()
    if limit <= 0:
        return
    now = time.time()
    key = _key(rule, identity)
    count, window_end = _with_fallback(rule, "peek", key, window_s, now)
    if count >= limit:
        retry_after = int(window_end - now) + 1
        _record_hit(rule, key, retry_after)
        raise RateLimitExceeded(rule, retry_after)


def record(rule: Rule, identity: Optional[str]) -> None:
    """Count one event for ``identity`` without refusing (failure-only rules)."""
    if not identity or not _enabled():
        return
    limit, window_s = rule.effective()
    if limit <= 0:
        return
    _with_fallback(rule, "hit", _key(rule, identity), window_s, time.time())


async def enforce_async(rule: Rule, identity: Optional[str]) -> None:
    """:func:`enforce` off the event loop."""
    await run_in_threadpool(enforce, rule, identity)


def normalize_username(username: Optional[str]) -> str:
    """Case- and whitespace-insensitive identity for per-account rules."""
    return (username or "").strip().lower()


def limit_by_ip(rule: Rule) -> Callable[[Request], None]:
    """FastAPI dependency charging ``rule`` per client IP.

    Declared as a plain ``def`` so FastAPI runs it in the threadpool and the
    Postgres round trip never blocks the event loop.
    """

    def dependency(request: Request) -> None:
        enforce(rule, client_ip_for_rate_limit(request))

    dependency.__name__ = f"rate_limit_{rule.name.replace('.', '_')}"
    return dependency


def limit_by_path_param(rule: Rule, param: str) -> Callable[[Request], None]:
    """FastAPI dependency charging ``rule`` per value of a path parameter."""

    def dependency(request: Request) -> None:
        value = request.path_params.get(param)
        enforce(rule, str(value) if value is not None else None)

    dependency.__name__ = f"rate_limit_{rule.name.replace('.', '_')}"
    return dependency


def install_rate_limit_handler(app: Any) -> None:
    """Render :class:`RateLimitExceeded` as the documented 429 body."""
    from fastapi.responses import JSONResponse

    async def handler(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RateLimitExceeded)
        return JSONResponse(
            status_code=exc.status_code, content=exc.body(), headers=exc.headers
        )

    app.add_exception_handler(RateLimitExceeded, handler)


__all__ = [
    "install_rate_limit_handler",
    "APPROVAL_PUBLIC_IP",
    "APPROVAL_PUBLIC_REQUEST",
    "AUTH_LOGIN_IP",
    "AUTH_LOGIN_USER",
    "AUTH_REFRESH",
    "AUTH_REGISTER",
    "AUTH_RESET_PASSWORD",
    "MemoryStore",
    "OAUTH_AUTHORIZE",
    "OAUTH_REGISTER",
    "OAUTH_TOKEN",
    "PostgresStore",
    "RATE_LIMITED_CODE",
    "RateLimitExceeded",
    "RateLimitStoreError",
    "Rule",
    "check_without_counting",
    "client_ip_for_rate_limit",
    "enforce",
    "enforce_async",
    "limit_by_ip",
    "limit_by_path_param",
    "normalize_username",
    "record",
    "reset_memory_store",
    "set_postgres_session_factory",
]
