"""Broker core for brokered live desktop views (#870).

Pure pieces (RFB client filter, throughput limiter, view tokens) plus the
message bus that joins the two halves of a relay. The viewer socket and the
endpoint socket may land on different API pods, so every hop between them goes
through a subject on the bus: NATS when it is connected, an in-process bus
otherwise (single replica installs and tests).

Subjects (``<pk>`` / ``<view>`` are UUIDs):

* ``desktop.endpoint.<pk>.cmd``   JSON to the pod holding the endpoint socket
* ``desktop.view.<view>.to_viewer``    raw RFB bytes, host -> viewer
* ``desktop.view.<view>.to_endpoint``  raw RFB bytes, viewer -> host
* ``desktop.view.<view>.ctl``          JSON control: close, mode, viewer_attached
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Limits from the issue.
MAX_FRAME_BYTES = 1024 * 1024
THROUGHPUT_BYTES_PER_SECOND = 8 * 1024 * 1024
DEFAULT_VIEW_SECONDS = 900
MIN_VIEW_SECONDS = 60
CONTROL_MAX_SECONDS = 600
VIEW_TOKEN_TTL_SECONDS = 15
VIEWER_IDLE_SECONDS = 120
VIEW_CREATIONS_PER_HOUR = 10
ENDPOINT_HEARTBEAT_SECONDS = 20
ENDPOINT_SILENCE_SECONDS = 60
#: Larger frames are split before publishing; NATS defaults to a 1 MB payload.
BUS_CHUNK_BYTES = 512 * 1024
#: Per-subscriber queue depth of the in-process bus (at most 32 MiB queued at
#: 512 KiB chunks). A full queue makes ``publish`` wait, so a stalled viewer
#: slows the host leg instead of growing memory.
LOCAL_BUS_QUEUE_SIZE = 64
#: NATS buffers at most this much per subscription; beyond it NATS drops the
#: slow consumer's messages (the view's RFB stream breaks and is reopened).
NATS_PENDING_BYTES_LIMIT = 32 * 1024 * 1024
MAX_CUT_TEXT_BYTES = 1024 * 1024

# Close codes (contract-870).
CLOSE_NORMAL = 1000
CLOSE_PROTOCOL = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_IDLE = 4408
CLOSE_VIEWER_BUSY = 4409
CLOSE_AGENT_DISCONNECTED = 4410
CLOSE_EXPIRED = 4411
CLOSE_USER_CLOSED = 4412
CLOSE_DISPLAY_UNAVAILABLE = 4413
CLOSE_UNREACHABLE = 4415
CLOSE_SERVER_RESTART = 4499
CLOSE_EVICTED = 4000

CLOSE_CODE_BY_REASON: dict[str, int] = {
    "user_closed": CLOSE_USER_CLOSED,
    "expired": CLOSE_EXPIRED,
    "idle": CLOSE_IDLE,
    "agent_disconnected": CLOSE_AGENT_DISCONNECTED,
    "viewer_disconnected": CLOSE_NORMAL,
    "desktop_agent_unreachable": CLOSE_UNREACHABLE,
    "display_unavailable": CLOSE_DISPLAY_UNAVAILABLE,
    "server_restart": CLOSE_SERVER_RESTART,
}
CLOSE_REASONS = tuple(CLOSE_CODE_BY_REASON)


# ---------------------------------------------------------------------------
# View tokens


def issue_view_token() -> tuple[str, str]:
    """Return ``(token, sha256 hex)``. Only the hash is stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_view_token(token)


def hash_view_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def view_token_matches(token: str, token_hash: Optional[str]) -> bool:
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_view_token(token), token_hash)


# ---------------------------------------------------------------------------
# View-only RFB filter


class RfbProtocolError(ValueError):
    """The viewer sent bytes that are not a valid RFB client stream."""


#: Client-to-server message types that carry user input.
INPUT_MESSAGE_TYPES = frozenset({4, 5, 6, 250, 251, 255})
#: Message types passed in view-only mode.
VIEW_MESSAGE_TYPES = frozenset({0, 2, 3, 150, 248})

_HANDSHAKE_VERSION = 0
_HANDSHAKE_SECURITY = 1
_HANDSHAKE_CLIENT_INIT = 2
_MESSAGES = 3


def _message_length(buf: bytes | bytearray) -> Optional[int]:
    """Full length of the client message at the start of ``buf``.

    None when more bytes are needed to know. Raises on unknown types.
    """
    if not buf:
        return None
    kind = buf[0]
    fixed = {0: 20, 3: 10, 4: 8, 5: 6, 150: 10, 250: 4}
    if kind in fixed:
        return fixed[kind]
    if kind == 2:  # SetEncodings: u8 type, pad, u16 count, s32 * count
        if len(buf) < 4:
            return None
        (count,) = struct.unpack(">H", bytes(buf[2:4]))
        return 4 + 4 * count
    if kind == 6:  # ClientCutText: u8, 3 pad, s32 length (negative = extended)
        if len(buf) < 8:
            return None
        (length,) = struct.unpack(">i", bytes(buf[4:8]))
        size = abs(length)
        if size > MAX_CUT_TEXT_BYTES:
            raise RfbProtocolError("ClientCutText too large")
        return 8 + size
    if kind == 248:  # Fence: u8, 3 pad, u32 flags, u8 length, payload
        if len(buf) < 9:
            return None
        return 9 + buf[8]
    if kind == 251:  # SetDesktopSize: u8, pad, u16 w, u16 h, u8 n, pad, 16 * n
        if len(buf) < 8:
            return None
        return 8 + 16 * buf[6]
    if kind == 255:  # QEMU: only the extended key event (subtype 0) is known
        if len(buf) < 2:
            return None
        if buf[1] == 0:
            return 12
        raise RfbProtocolError(f"unsupported QEMU client message {buf[1]}")
    raise RfbProtocolError(f"unknown RFB client message type {kind}")


class RfbClientFilter:
    """Parses the viewer's RFB stream and drops input in view-only mode.

    The API side of the relay always speaks RFB 3.7/3.8 with security type
    None, so the client stream is: 12 byte ProtocolVersion, 1 byte security
    type, 1 byte ClientInit, then client messages. Incomplete messages are held
    until the rest arrives so a split frame can never smuggle input through.
    """

    def __init__(self) -> None:
        self._phase = _HANDSHAKE_VERSION
        self._buf = bytearray()
        self.dropped: dict[int, int] = defaultdict(int)

    def feed(self, data: bytes, *, allow_input: bool) -> bytes:
        """Consume viewer bytes; return the bytes to forward to the host."""
        self._buf.extend(data)
        out = bytearray()
        while True:
            if self._phase == _HANDSHAKE_VERSION:
                if len(self._buf) < 12:
                    break
                version = bytes(self._buf[:12])
                if version not in (b"RFB 003.008\n", b"RFB 003.007\n"):
                    raise RfbProtocolError("unsupported RFB version")
                out.extend(version)
                del self._buf[:12]
                self._phase = _HANDSHAKE_SECURITY
            elif self._phase == _HANDSHAKE_SECURITY:
                if not self._buf:
                    break
                if self._buf[0] != 1:
                    raise RfbProtocolError("only security type None is offered")
                out.append(self._buf[0])
                del self._buf[:1]
                self._phase = _HANDSHAKE_CLIENT_INIT
            elif self._phase == _HANDSHAKE_CLIENT_INIT:
                if not self._buf:
                    break
                out.append(self._buf[0])
                del self._buf[:1]
                self._phase = _MESSAGES
            else:
                length = _message_length(self._buf)
                if length is None or len(self._buf) < length:
                    break
                kind = self._buf[0]
                message = bytes(self._buf[:length])
                del self._buf[:length]
                if allow_input or kind in VIEW_MESSAGE_TYPES:
                    out.extend(message)
                else:
                    self.dropped[kind] += 1
        if len(self._buf) > MAX_FRAME_BYTES + MAX_CUT_TEXT_BYTES:
            raise RfbProtocolError("buffered client data too large")
        return bytes(out)


# ---------------------------------------------------------------------------
# Throughput cap


class ThroughputLimiter:
    """Token bucket that paces a byte stream to ``rate`` bytes per second."""

    def __init__(
        self,
        rate: int = THROUGHPUT_BYTES_PER_SECOND,
        burst: int = MAX_FRAME_BYTES,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.rate = rate
        self.burst = max(burst, 1)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self.burst)
        self._last = clock()

    async def acquire(self, size: int) -> float:
        """Wait until ``size`` bytes may pass; return the seconds waited."""
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now
        self._tokens -= size
        if self._tokens >= 0:
            return 0.0
        delay = -self._tokens / self.rate
        await self._sleep(delay)
        return delay


# ---------------------------------------------------------------------------
# Bus

Handler = Callable[[bytes], Awaitable[None]]
Unsubscribe = Callable[[], Awaitable[None]]


class LocalDesktopBus:
    """In-process pub/sub with ordered, bounded per-subscriber delivery."""

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[Optional[bytes]]]] = defaultdict(list)

    async def subscribe(self, subject: str, handler: Handler) -> Unsubscribe:
        queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(LOCAL_BUS_QUEUE_SIZE)
        self._subs[subject].append(queue)

        async def pump() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                try:
                    await handler(item)
                except Exception:  # noqa: BLE001 - keep the subscription alive
                    logger.error("desktop bus handler failed on %s", subject)

        task = asyncio.create_task(pump())

        async def unsubscribe() -> None:
            subs = self._subs.get(subject, [])
            if queue in subs:
                subs.remove(queue)
            if not subs:
                self._subs.pop(subject, None)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        return unsubscribe

    async def publish(self, subject: str, data: bytes) -> None:
        for queue in list(self._subs.get(subject, [])):
            await queue.put(data)


class NatsDesktopBus:
    """Bus over a connected NATS client, for multi-replica deployments."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def subscribe(self, subject: str, handler: Handler) -> Unsubscribe:
        async def callback(msg: Any) -> None:
            await handler(msg.data)

        sub = await self._client.subscribe(
            subject, cb=callback, pending_bytes_limit=NATS_PENDING_BYTES_LIMIT
        )

        async def unsubscribe() -> None:
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001 - connection may already be gone
                logger.debug("desktop bus unsubscribe failed", exc_info=True)

        return unsubscribe

    async def publish(self, subject: str, data: bytes) -> None:
        await self._client.publish(subject, data)


_local_bus = LocalDesktopBus()
_bus_override: Any = None


def set_desktop_bus(bus: Any) -> None:
    """Tests pin a bus; ``None`` restores automatic selection."""
    global _bus_override
    _bus_override = bus


async def get_desktop_bus() -> Any:
    if _bus_override is not None:
        return _bus_override
    try:
        from preloop.sync.services.event_bus import get_nats_client

        client = await get_nats_client()
    except Exception:  # noqa: BLE001 - NATS is optional for single replicas
        client = None
    if client is not None and getattr(client, "is_connected", False):
        return NatsDesktopBus(client)
    return _local_bus


def endpoint_cmd_subject(endpoint_pk: Any) -> str:
    return f"desktop.endpoint.{endpoint_pk}.cmd"


def view_subject(view_id: Any, leg: str) -> str:
    return f"desktop.view.{view_id}.{leg}"


async def publish_bytes(bus: Any, subject: str, data: bytes) -> None:
    for start in range(0, len(data), BUS_CHUNK_BYTES):
        await bus.publish(subject, data[start : start + BUS_CHUNK_BYTES])


async def publish_json(bus: Any, subject: str, payload: dict[str, Any]) -> None:
    await bus.publish(subject, json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# State machine helpers

#: Allowed transitions; anything else is refused.
VIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"live", "closed"}),
    "live": frozenset({"closed"}),
    "closed": frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in VIEW_TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True)
class AuditFacts:
    """Fields every desktop audit row carries."""

    view_id: str
    endpoint_id: str
    hostname: str
    managed_agent_id: Optional[str]
    runner_id: Optional[str]
    runtime_session_id: Optional[str]
    mode: str


def view_ended_details(
    facts: AuditFacts,
    *,
    live_at: Optional[datetime],
    closed_at: datetime,
    closed_reason: str,
) -> dict[str, Any]:
    """Payload for ``desktop.view.ended``; never includes secrets."""
    duration = (closed_at - live_at).total_seconds() if live_at else 0.0
    return {
        **facts.__dict__,
        "duration_seconds": round(max(duration, 0.0), 3),
        "closed_reason": closed_reason,
    }
