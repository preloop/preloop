"""Bounded, isolated tracker connection tests built from caller input.

The tracker test, list and register endpoints build a client for a URL the
caller supplies and talk to it while the caller waits. Several clients make
synchronous HTTP calls (python-gitlab and the Jira library authenticate in
their constructors), so running them on the API event loop lets one host that
accepts a connection and never answers freeze every request on the worker,
including the liveness probe.

:func:`run_tracker_probe` runs that work somewhere it cannot do harm:

* Each probe gets its own thread from a small dedicated pool and its own event
  loop with its own small executor. Nothing runs on the API loop or in its
  default executor, and the API loop only awaits a future.
* At most :data:`PROBE_MAX_CONCURRENCY` probes run per process; another one is
  refused at once with :class:`TrackerProbeBusyError` instead of queueing.
* Client calls inside a probe use :func:`probe_timeout` (connect and read
  timeouts), and the whole probe has one deadline, :data:`PROBE_DEADLINE_S`.
  A slot is only released when the probe's threads have really finished, so a
  call that ignores cancellation still cannot pile up.
* Every name resolution and every connection made by a probe goes through a
  guard that checks the target address against the outbound policy and pins
  the answer, so a redirect or a second DNS answer cannot reach an address
  that the first check refused.

The policy is set by ``tracker_block_private_targets``. Off (the default, for
self-hosted installs that point at a private GitLab or Jira) it refuses only
link-local and cloud metadata, unspecified and multicast addresses. On it also
refuses loopback, private, unique-local, reserved and other non-global
addresses and cluster-internal host names.
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import socket
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib.parse import urlsplit

from preloop.config import settings
from preloop.services.event_webhooks.targets import blocked_reason_for_address
from preloop.services.rate_limit import Rule
from preloop.utils.bitbucket_dc import METADATA_NETWORKS

T = TypeVar("T")

PROBE_CONNECT_TIMEOUT_S = 3.0
"""Seconds to establish one TCP (and TLS) connection to a tracker."""
PROBE_READ_TIMEOUT_S = 5.0
"""Seconds to wait for bytes on an open tracker connection."""
PROBE_DEADLINE_S = 8.0
"""Seconds one whole connection test (all of its calls) may take."""
PROBE_MAX_CONCURRENCY = 4
"""Connection tests one API process runs at the same time."""
PROBE_IO_THREADS = 4
"""Worker threads of each probe's private event loop."""
PROBE_BUSY_RETRY_AFTER_S = 5
"""``Retry-After`` sent with the busy refusal."""

# Per user. ``RATE_LIMIT_TRACKERS_CONNECTION_TEST=<limit>/<seconds>`` overrides.
TRACKER_CONNECTION_TEST = Rule("trackers.connection_test", 20, 60)

ALLOWED_SCHEMES = ("http", "https")
# Host names that only make sense inside the cluster or on the host itself.
_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".svc",
    ".cluster.local",
    ".localdomain",
)


class TrackerProbeError(Exception):
    """Base class for refusals raised by :func:`run_tracker_probe`."""


class TrackerProbeBusyError(TrackerProbeError):
    """Every connection test slot of this process is in use."""


class TrackerProbeTimeoutError(TrackerProbeError):
    """The tracker did not answer within the probe's timeouts."""


class TrackerTargetRefusedError(TrackerProbeError, socket.gaierror):
    """The tracker URL or an address it resolves to is not allowed.

    Also a ``socket.gaierror`` so HTTP libraries treat a refusal raised from
    inside name resolution as a failed lookup and stop there.
    """


@dataclass
class ProbePolicy:
    """Outbound policy and pinned DNS answers for one probe.

    Attributes:
        strict: Refuse private and internal destinations as well.
        pinned: Host name to the checked addresses it resolved to.
        refusal: The first refusal, kept so it survives client error wrapping.
    """

    strict: bool
    pinned: Dict[str, List[str]] = field(default_factory=dict)
    refusal: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _refuse(self, reason: str) -> TrackerTargetRefusedError:
        with self._lock:
            if self.refusal is None:
                self.refusal = reason
        return TrackerTargetRefusedError(socket.EAI_NONAME, reason)

    def address_reason(self, raw: str) -> Optional[str]:
        """Why one address may not be contacted under this policy, or None.

        Args:
            raw: Address text (an IPv6 zone suffix is ignored).

        Returns:
            A short reason such as ``"loopback"``, or None when allowed.
        """
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return "invalid address"
        mapped = getattr(address, "ipv4_mapped", None)
        effective = mapped if mapped is not None else address
        for network in METADATA_NETWORKS:
            for candidate in (address, effective):
                if candidate.version == network.version and candidate in network:
                    return "link-local or metadata"
        if effective.is_link_local:
            return "link-local or metadata"
        if effective.is_unspecified:
            return "unspecified"
        if effective.is_multicast:
            return "multicast"
        if self.strict:
            return blocked_reason_for_address(str(effective))
        return None

    def host_reason(self, host: str) -> Optional[str]:
        """Why a host name may not be contacted under this policy, or None."""
        if not self.strict:
            return None
        name = host.lower().rstrip(".")
        if name == "localhost" or name.endswith(_INTERNAL_SUFFIXES):
            return "internal host name"
        if "." not in name:
            # Single-label names resolve through the cluster search path to
            # in-cluster services (postgres, nats, the API itself).
            return "internal host name"
        return None

    def addresses_for(self, host: str) -> List[str]:
        """Return the checked addresses for ``host``, resolving at most once.

        Every address in the answer is checked; one refused address refuses
        the whole name. The answer is pinned for the rest of the probe.

        Args:
            host: Host name or address literal.

        Returns:
            The addresses connections to ``host`` may use.

        Raises:
            TrackerTargetRefusedError: When the name or any address is refused.
            socket.gaierror: When the name does not resolve.
        """
        name = host.strip("[]").lower().rstrip(".")
        try:
            ipaddress.ip_address(name.split("%", 1)[0])
        except ValueError:
            pass
        else:
            reason = self.address_reason(name)
            if reason:
                raise self._refuse(f"address is {reason}")
            return [name]
        with self._lock:
            cached = self.pinned.get(name)
        if cached is not None:
            return list(cached)
        reason = self.host_reason(name)
        if reason:
            raise self._refuse(reason)
        try:
            infos = _SYSTEM_GETADDRINFO(name, None, 0, socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            # Not a refusal: the client reports a name that does not resolve
            # as an ordinary connection failure.
            raise socket.gaierror(socket.EAI_NONAME, "unresolvable") from exc
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        if not addresses:
            raise socket.gaierror(socket.EAI_NONAME, "unresolvable")
        for address in addresses:
            reason = self.address_reason(address)
            if reason:
                raise self._refuse(f"host resolves to a {reason} address")
        with self._lock:
            return list(self.pinned.setdefault(name, addresses))


# ---------------------------------------------------------------------------
# Resolution and connection guard
# ---------------------------------------------------------------------------

_state = threading.local()
_SYSTEM_GETADDRINFO = socket.getaddrinfo


def current_policy() -> Optional[ProbePolicy]:
    """The policy of the probe running on this thread, or None."""
    return getattr(_state, "policy", None)


def probe_timeout() -> Optional[Tuple[float, float]]:
    """``(connect, read)`` seconds inside a probe, else None.

    Clients pass this to ``requests``-based libraries so their calls are
    bounded in a probe while saved-tracker sync keeps its own behaviour.
    """
    if current_policy() is None:
        return None
    return (PROBE_CONNECT_TIMEOUT_S, PROBE_READ_TIMEOUT_S)


def probe_httpx_timeout(default: Any) -> Any:
    """An ``httpx`` timeout for this thread: the probe's inside one, else ``default``."""
    if current_policy() is None:
        return default
    import httpx

    return httpx.Timeout(PROBE_READ_TIMEOUT_S, connect=PROBE_CONNECT_TIMEOUT_S)


def _guarded_getaddrinfo(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> List[Any]:
    """``socket.getaddrinfo`` that answers from the probe's checked addresses.

    Outside a probe thread it is the system function. Inside one, the host
    is checked and pinned by the probe policy, and only the pinned addresses
    are returned, so whatever connects next connects to a checked address.
    """
    policy = current_policy()
    if policy is None or host is None:
        return _SYSTEM_GETADDRINFO(host, port, family, type, proto, flags)
    name = host.decode("idna") if isinstance(host, bytes) else str(host)
    results: List[Any] = []
    for address in policy.addresses_for(name):
        try:
            results.extend(
                _SYSTEM_GETADDRINFO(
                    address, port, family, type, proto, flags | socket.AI_NUMERICHOST
                )
            )
        except socket.gaierror:
            continue  # address family filtered out by the caller
    if not results:
        raise socket.gaierror(socket.EAI_NONAME, "no usable address")
    return results


def install_resolution_guard() -> None:
    """Route ``socket.getaddrinfo`` through the probe guard (idempotent).

    Only threads running a probe are affected; every other caller gets the
    system resolver unchanged.
    """
    if socket.getaddrinfo is not _guarded_getaddrinfo:
        socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]


class _ProbeEventLoop(asyncio.SelectorEventLoop):
    """Event loop for one probe that checks literal connection targets.

    Names already go through the guarded resolver in the loop's executor.
    asyncio and anyio skip resolution for address literals, for example after
    a redirect to ``http://10.0.0.1/``, so those are checked here.
    """

    async def create_connection(  # type: ignore[override]
        self, protocol_factory: Any, host: Any = None, port: Any = None, **kwargs: Any
    ) -> Any:
        policy = current_policy()
        if policy is not None and host is not None:
            policy.addresses_for(str(host))
        return await super().create_connection(protocol_factory, host, port, **kwargs)


def _enter_probe_thread(policy: ProbePolicy) -> None:
    _state.policy = policy


def validate_target_url(url: Optional[str], policy: ProbePolicy) -> None:
    """Check a caller-supplied tracker URL before any connection.

    Args:
        url: The tracker URL, or None when the client uses its default host.
        policy: The probe's policy; resolution results are pinned on it.

    Raises:
        TrackerTargetRefusedError: For another scheme, embedded credentials, a
            refused host name, or any refused resolved address.
    """
    if url is None or str(url).strip() == "":
        return
    try:
        parts = urlsplit(str(url).strip())
        host = parts.hostname
        _ = parts.port  # raises on a malformed port
    except ValueError as exc:
        raise policy._refuse("malformed URL") from exc
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise policy._refuse("URL must use http or https")
    if not host:
        raise policy._refuse("URL has no host")
    if parts.username is not None or parts.password is not None:
        raise policy._refuse("URL must not contain credentials")
    try:
        policy.addresses_for(host)
    except TrackerTargetRefusedError:
        raise
    except socket.gaierror:
        # Leave an unresolvable name to the client, whose connection still
        # goes through the guard, so a typo keeps its usual error message.
        return


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_slots = threading.BoundedSemaphore(PROBE_MAX_CONCURRENCY)
_executor = ThreadPoolExecutor(
    max_workers=PROBE_MAX_CONCURRENCY, thread_name_prefix="tracker-probe"
)
_TIMEOUT_TYPES: Tuple[type, ...] = (TimeoutError, socket.timeout)


def _timeout_types() -> Tuple[type, ...]:
    extra: List[type] = []
    try:
        import requests

        extra.append(requests.Timeout)
    except ImportError:  # pragma: no cover - requests is a core dependency
        pass
    try:
        import httpx

        extra.append(httpx.TimeoutException)
    except ImportError:  # pragma: no cover - httpx is a core dependency
        pass
    return _TIMEOUT_TYPES + tuple(extra)


def _caused_by_timeout(exc: BaseException) -> bool:
    types = _timeout_types()
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, types):
            return True
        current = current.__cause__ or current.__context__
    return False


def _translate(exc: BaseException, policy: ProbePolicy) -> BaseException:
    """Map a probe failure to a refusal, a timeout, or the original error."""
    if isinstance(exc, TrackerProbeError):
        return exc
    if policy.refusal is not None:
        refused = TrackerTargetRefusedError(socket.EAI_NONAME, policy.refusal)
        refused.__cause__ = exc
        return refused
    if isinstance(exc, asyncio.TimeoutError) or _caused_by_timeout(exc):
        timeout = TrackerProbeTimeoutError("tracker did not respond in time")
        timeout.__cause__ = exc
        return timeout
    return exc


def _run_isolated(
    work: Callable[[], Awaitable[T]],
    url: Optional[str],
    policy: ProbePolicy,
    deadline: float,
) -> T:
    """Run ``work`` on a private event loop on this (probe) thread."""
    _enter_probe_thread(policy)
    loop = _ProbeEventLoop()
    io = ThreadPoolExecutor(
        max_workers=PROBE_IO_THREADS,
        thread_name_prefix="tracker-probe-io",
        initializer=_enter_probe_thread,
        initargs=(policy,),
    )
    loop.set_default_executor(io)
    asyncio.set_event_loop(loop)

    async def main() -> T:
        await loop.run_in_executor(None, validate_target_url, url, policy)
        return await work()

    try:
        return loop.run_until_complete(asyncio.wait_for(main(), deadline))
    except BaseException as exc:
        raise _translate(exc, policy)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            # Waits for calls still running in the probe's own threads; each is
            # bounded by probe_timeout(), and the slot stays taken until then.
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
            _state.policy = None


async def run_tracker_probe(
    work: Callable[[], Awaitable[T]],
    *,
    url: Optional[str],
    isolate: bool = True,
    deadline: float = PROBE_DEADLINE_S,
) -> T:
    """Run one tracker connection test inside the probe bounds.

    Args:
        work: Zero-argument coroutine function that builds the client and
            talks to the tracker. With ``isolate`` it runs on a private event
            loop in another thread, so it must not touch the request's
            database session or objects bound to the API loop.
        url: The caller-supplied tracker URL, validated before ``work`` runs.
        isolate: Run on a private loop and thread under the address guard.
            Managed connections and GitHub App installations, whose origin is
            fixed by the provider and whose credential service is bound to the
            API loop, pass False: their URL is still validated up front, and
            they get the deadline and the concurrency bound, but their
            connections are not pinned. Their clients are fully async.
        deadline: Seconds the whole test may take.

    Returns:
        Whatever ``work`` returns.

    Raises:
        TrackerProbeBusyError: All slots are in use.
        TrackerProbeTimeoutError: The tracker did not answer within the bounds.
        TrackerTargetRefusedError: The URL or a resolved address is not allowed.
        Exception: Any other error raised by ``work``.
    """
    if not _slots.acquire(blocking=False):
        raise TrackerProbeBusyError("too many tracker connection tests in progress")
    policy = ProbePolicy(strict=bool(settings.tracker_block_private_targets))
    if not isolate:
        try:
            # A slot is held, so a probe worker is free for the DNS lookup;
            # it never runs on the API loop or its default executor.
            await asyncio.wait_for(
                asyncio.wrap_future(_executor.submit(validate_target_url, url, policy)),
                deadline,
            )
            return await asyncio.wait_for(work(), deadline)
        except BaseException as exc:
            raise _translate(exc, policy)
        finally:
            _slots.release()
    install_resolution_guard()
    context = contextvars.copy_context()
    try:
        future: Future[T] = _executor.submit(
            context.run, _run_isolated, work, url, policy, deadline
        )
    except BaseException:
        _slots.release()
        raise
    future.add_done_callback(lambda _: _slots.release())
    try:
        # The private loop enforces the deadline; this margin only covers a
        # client call that ignored its own timeout.
        return await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(future)), deadline + 2.0
        )
    except asyncio.TimeoutError:
        raise TrackerProbeTimeoutError("tracker did not respond in time") from None


__all__ = [
    "PROBE_CONNECT_TIMEOUT_S",
    "PROBE_DEADLINE_S",
    "PROBE_MAX_CONCURRENCY",
    "PROBE_READ_TIMEOUT_S",
    "ProbePolicy",
    "TRACKER_CONNECTION_TEST",
    "TrackerProbeBusyError",
    "TrackerProbeError",
    "TrackerProbeTimeoutError",
    "TrackerTargetRefusedError",
    "current_policy",
    "install_resolution_guard",
    "probe_httpx_timeout",
    "probe_timeout",
    "run_tracker_probe",
    "validate_target_url",
]
