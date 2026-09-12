"""Bounded gateway failure alerts with fleet-wide incident reservations.

Local windows keep broker load to one reservation per incident per process per
quiet window. Eligible alerts compete for an atomic, expiring JetStream KV key
in the background notification worker. Broker failures fall back to the already
reserved local window. Reminder suppression counts describe this process only.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import ipaddress
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Quiet period (seconds) between admin alerts for one incident key.
DEFAULT_ALERT_INTERVAL_SECONDS = 300.0

#: Env var overriding the quiet period; must parse to a finite positive number.
GATEWAY_ERROR_ALERT_INTERVAL_ENV = "GATEWAY_ERROR_ALERT_INTERVAL_SECONDS"

#: Only 5xx notifications use the throttle; anything below is never reserved.
_MIN_THROTTLED_STATUS = 500

_lock = threading.Lock()
_state: dict[str, "_Window"] = {}
_MAX_LOCAL_WINDOWS = 4096
_SHARED_TIMEOUT_SECONDS = 1.0
_SHARED_MAX_BYTES = 4 * 1024 * 1024

#: Monotonic clock, overridable in tests (``time.monotonic`` in production).
_clock: Callable[[], float] = time.monotonic


@dataclass
class _Window:
    """Quiet window for one stable incident key.

    Attributes:
        next_allowed_at: Earliest monotonic time (inclusive) at which the
            next alert for this key may be sent.
        suppressed: Number of 5xx alerts dropped while this window was open.
    """

    next_allowed_at: float
    suppressed: int = 0


def _alert_interval_seconds() -> float:
    """Return the configured quiet window, falling back to the default.

    The env var must be a finite positive number of seconds; missing, empty,
    non-numeric, infinite, NaN, zero, and negative values all fall back to
    :data:`DEFAULT_ALERT_INTERVAL_SECONDS`.

    Returns:
        The quiet window length in seconds.
    """
    raw = os.getenv(GATEWAY_ERROR_ALERT_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_ALERT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ALERT_INTERVAL_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_ALERT_INTERVAL_SECONDS
    return value


def gateway_alert_key(
    provider: str,
    status_code: int,
    *,
    account_id: str = "",
    upstream_provider: str = "",
    model: str = "",
    model_id: str = "",
    error_class: str = "",
    upstream_status: int | None = None,
) -> str:
    """Hash stable incident identity without storing account/model data in NATS.

    Do not include exception messages or request IDs: they can contain secrets
    and vary for each occurrence of the same outage.
    """
    identity = [
        str(account_id),
        provider,
        upstream_provider,
        model,
        str(model_id),
        error_class,
        status_code,
        upstream_status,
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def gateway_outage_key(
    provider: str,
    *,
    upstream_status: int | None,
    error_class: str,
    endpoint: str = "",
    account_id: str = "",
    model_id: str = "",
) -> str | None:
    """Budget general upstream outages across accounts, models and HTTP codes.

    Account-specific auth/quota failures retain their granular incident keys.
    Private addresses and endpoint-less generic adapters cannot identify a
    common public upstream, so their budgets stay scoped to the configuration.
    No DNS requests or credential-bearing URL parts enter the identity.
    """
    if error_class in {
        "upstream_auth",
        "upstream_quota_exhausted",
        "upstream_rate_limited",
    }:
        return None
    if not (
        (upstream_status is not None and 500 <= upstream_status <= 599)
        or error_class in {"network", "upstream_overloaded"}
    ):
        return None
    upstream = provider.strip().lower()
    private = False
    canonical = ""
    if endpoint:
        try:
            parsed = urlsplit(endpoint.strip())
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme.lower() not in {"http", "https"} or not host:
                raise ValueError("Invalid upstream URL")
            port = parsed.port
            if port == {"http": 80, "https": 443}.get(parsed.scheme.lower()):
                port = None
            canonical = json.dumps(
                [parsed.scheme.lower(), host, port, parsed.path.rstrip("/")]
            )
            try:
                private = not ipaddress.ip_address(host).is_global
            except ValueError:
                private = "." not in host or host.endswith(
                    (".local", ".localhost", ".internal")
                )
        except ValueError:
            private = True
    elif upstream in {
        "",
        "custom",
        "openai-compatible",
        "openai_compatible",
        "ollama",
        "vllm",
        "local",
    }:
        private = True
    identity = ["availability", upstream, canonical]
    if private:
        identity.extend([str(account_id), str(model_id)])
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class _SharedAlertStore:
    """One connection and current bucket, owned by one running event loop."""

    def __init__(self) -> None:
        self.connection: Any = None
        self.store: Any = None
        self.configuration: tuple[str, float] | None = None
        self._operation_lock = asyncio.Lock()

    async def close(self) -> None:
        """Wait for in-flight reservation cleanup before closing its client."""
        async with self._operation_lock:
            await self._close()

    async def _close(self) -> None:
        """Clear state before bounded cleanup, even if closing fails."""
        connection, self.connection = self.connection, None
        self.store = None
        self.configuration = None
        if connection is not None:
            try:
                await asyncio.wait_for(connection.close(), timeout=0.25)
            except Exception:
                logger.warning(
                    "Could not close gateway alert connection", exc_info=True
                )

    async def reserve(self, incident_key: str, interval: float) -> bool:
        """Serialize reservation and shutdown on the owning event loop."""
        async with self._operation_lock:
            return await self._reserve(incident_key, interval)

    async def _reserve(self, incident_key: str, interval: float) -> bool:
        """Reserve one broker-expiring slot; reconnect only on a later call."""
        import nats
        from nats.js.api import KeyValueConfig, StorageType
        from nats.js.errors import BucketNotFoundError, KeyWrongLastSequenceError

        from preloop.config import settings

        try:
            async with asyncio.timeout(_SHARED_TIMEOUT_SECONDS):
                configuration = (settings.nats_url, interval)
                if self.connection is not None and (
                    not self.connection.is_connected
                    or self.configuration != configuration
                ):
                    await self._close()
                if self.connection is None:
                    # Own the client before its first network await, so a
                    # timed-out handshake cannot orphan an open transport.
                    self.connection = nats.NATS()
                    await self.connection.connect(
                        settings.nats_url,
                        name="preloop-gateway-alerts",
                        connect_timeout=0.25,
                        max_reconnect_attempts=0,
                        allow_reconnect=False,
                    )
                    self.configuration = configuration
                if self.store is None:
                    jetstream = self.connection.jetstream(timeout=0.5)
                    interval_hash = hashlib.sha256(str(interval).encode()).hexdigest()[
                        :12
                    ]
                    bucket = f"gateway_alerts_{interval_hash}"
                    try:
                        self.store = await jetstream.key_value(bucket)
                    except BucketNotFoundError:
                        self.store = await jetstream.create_key_value(
                            KeyValueConfig(
                                bucket=bucket,
                                ttl=interval,
                                history=1,
                                max_bytes=_SHARED_MAX_BYTES,
                                max_value_size=1024,
                                storage=StorageType.FILE,
                            )
                        )
                try:
                    await self.store.create(incident_key, b"1")
                except KeyWrongLastSequenceError:
                    return False
                return True
        except BaseException:
            # Includes timeout cancellation: never reuse an uncertain client.
            await self._close()
            raise


async def _reserve_shared_alert(incident_key: str, interval: float) -> bool:
    """Single-use reservation helper for isolated clients and broker checks."""
    store = _SharedAlertStore()
    try:
        return await store.reserve(incident_key, interval)
    finally:
        await store.close()


class _SharedAlertWorker:
    """Keep NATS heartbeats running while the notification worker is idle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._store = _SharedAlertStore()
        self._pending: set[Any] = set()

    @property
    def closed(self) -> bool:
        """Whether shutdown has rejected further submissions."""
        return self._closed

    def reserve(self, incident_key: str, interval: float) -> bool:
        """Called only from the bounded notification executor, never requests."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Gateway alert worker has shut down")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever,
                    name="gateway-alert-nats",
                    daemon=True,
                )
                self._thread.start()
            future = asyncio.run_coroutine_threadsafe(
                self._store.reserve(incident_key, interval), self._loop
            )
            self._pending.add(future)
        try:
            return future.result(timeout=_SHARED_TIMEOUT_SECONDS + 0.5)
        except TimeoutError:
            future.cancel()
            raise
        finally:
            with self._lock:
                self._pending.discard(future)

    def close(self) -> None:
        """Reject late work, close the client, then stop its event loop."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
            if loop is None:
                return
            for pending in self._pending:
                pending.cancel()
            cleanup = asyncio.run_coroutine_threadsafe(self._store.close(), loop)
            # Even if the caller's bounded wait expires, cleanup finishes on
            # the running loop before stopping it. Never cut a client close short.
            cleanup.add_done_callback(lambda _: loop.call_soon_threadsafe(loop.stop))
        try:
            cleanup.result(timeout=1.0)
        except Exception:
            logger.warning("Gateway alert worker cleanup failed", exc_info=True)
        finally:
            if thread is not None:
                thread.join(timeout=0.5)
            if not loop.is_running():
                loop.close()


_SHARED_WORKER = _SharedAlertWorker()
atexit.register(_SHARED_WORKER.close)


def _reserve_shared_or_fallback(incident_key: str) -> bool:
    """Fall back to the caller's local reservation if the broker is unavailable."""
    try:
        return _SHARED_WORKER.reserve(incident_key, _alert_interval_seconds())
    except Exception:
        logger.warning(
            "Shared gateway alert reservation unavailable; using local cooldown",
            exc_info=True,
        )
        return True


def reserve_gateway_5xx_alert(
    provider: str,
    status_code: int,
    *,
    now: Optional[float] = None,
    incident_key: str | None = None,
) -> Tuple[bool, int]:
    """Atomically reserve an admin-alert slot for a gateway 5xx.

    The first error for an incident key sends immediately and opens a quiet
    window; repeats inside the window are dropped. The
    first error after the window closes sends again and reports how many
    alerts were suppressed while it was open; the counter then resets for the
    new window. Statuses below 500 are never reserved.

    The window is reserved before the notifier runs so a failing notifier
    consumes its slot instead of re-arming the alert.

    Args:
        provider: Gateway provider value used for alert keying (e.g.
            ``"openai"``).
        status_code: Normalized integer HTTP status of the failure.
        now: Monotonic timestamp override for tests. Defaults to the module
            clock (``time.monotonic``).
        incident_key: Stable identity from :func:`gateway_alert_key`. Legacy
            callers default to protocol and status only.

    Returns:
        A ``(send, suppressed)`` pair: ``send`` is True when the caller
        should emit the notification now, and ``suppressed`` is the number of
        alerts dropped during the previous window (always 0 when ``send`` is
        False, and on the very first alert for a key).
    """
    try:
        if status_code < _MIN_THROTTLED_STATUS:
            return False, 0
        key = incident_key or gateway_alert_key(str(provider), int(status_code))
        interval = _alert_interval_seconds()
        current = _clock() if now is None else now

        with _lock:
            window = _state.get(key)
            if window is None or current >= window.next_allowed_at:
                suppressed = window.suppressed if window is not None else 0
                if window is None and len(_state) >= _MAX_LOCAL_WINDOWS:
                    # Preserve active reservations instead of evicting them and
                    # re-arming noisy incidents. Expired entries need no cleanup
                    # task; a new incident reclaims their space here.
                    expired = [
                        k
                        for k, value in _state.items()
                        if current >= value.next_allowed_at
                    ]
                    for expired_key in expired:
                        del _state[expired_key]
                    if len(_state) >= _MAX_LOCAL_WINDOWS:
                        return False, 0
                _state[key] = _Window(next_allowed_at=current + interval)
                return True, suppressed
            window.suppressed += 1
            return False, 0
    except Exception:  # noqa: BLE001 - alerting must never break the gateway
        # A bug in the throttle must not silently silence outage alerts: fall
        # back to the pre-throttle behavior (send now) and log a breadcrumb.
        logger.warning(
            "Gateway 5xx alert throttle failed for %s; sending unsuppressed",
            provider,
            exc_info=True,
        )
        return True, 0


def reset_alert_state_for_tests() -> None:
    """Clear the in-process quiet windows (test isolation only)."""
    with _lock:
        _state.clear()


# Notifications perform network I/O; keep them off request/stream threads.
# Bound both queued and running work so a slow notifier cannot grow memory.
_ALERT_PENDING = threading.BoundedSemaphore(32)
_ALERT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-alert")
atexit.register(_ALERT_EXECUTOR.shutdown, wait=False, cancel_futures=True)


def enqueue_gateway_5xx_alert(
    *, subject: str, message: str, incident_key: str | None = None
) -> None:
    """Schedule best-effort delivery without delaying the gateway response.

    The caller must reserve its quiet window first. A full queue or a failed
    delivery consumes that window; alert failures must not cause retry storms.
    """
    if _SHARED_WORKER.closed:
        logger.debug("Gateway alert worker has shut down; dropping notification")
        return
    if not _ALERT_PENDING.acquire(blocking=False):
        logger.warning("Gateway alert queue is full; dropping notification")
        return

    def _deliver() -> None:
        try:
            from preloop.sync.tasks import notify_admins

            if incident_key is not None and not _reserve_shared_or_fallback(
                incident_key
            ):
                return
            notify_admins(subject=subject, message=message)
        except Exception:
            logger.warning("Gateway admin notification failed", exc_info=True)
        finally:
            _ALERT_PENDING.release()

    try:
        _ALERT_EXECUTOR.submit(_deliver)
    except Exception:
        _ALERT_PENDING.release()
        logger.warning("Could not schedule gateway admin notification", exc_info=True)
