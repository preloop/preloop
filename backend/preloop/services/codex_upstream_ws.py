"""Upstream Responses WebSocket transport for the Codex OAuth path (#1454).

Preloop keeps receiving full HTTP bodies from Codex, so every gateway hook
(policy, redaction, audit, budget, tool activity, cross-chat, operator notes)
still sees the whole thread. Only the hop to
``chatgpt.com/backend-api/codex/responses`` changes: one socket per
``(account, session-id)`` is kept warm in this pod, and each call is diffed
against what was last sent on that socket using the same rule as the Codex
CLI (openai/codex ``codex-rs/core/src/client.rs`` 383-449 and 1458-1521):

* a call goes *incremental* only when the socket already carries a request
  and its response, no request property changed, and the new ``input``
  starts with exactly the old input followed by the items that response
  added;
* an incremental frame is ``response.create`` with ``previous_response_id``
  and only the new items; otherwise the full input is sent.

The upstream holds response state in a connection-local cache, so pinning a
thread to one socket also pins it to one upstream server (cache affinity).

This module holds the transport only: the registry, the diff rule and the
socket I/O. Wiring and fallbacks live in
``OpenAIGatewayService._create_openai_codex_response``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

CODEX_WS_URL = "wss://chatgpt.com/backend-api/codex/responses"
#: Beta header the Codex CLI sends on the WebSocket handshake (client.rs:175).
CODEX_WS_BETA = "responses_websockets=2026-02-06"
TURN_STATE_KEY = "x-codex-turn-state"
PREVIOUS_RESPONSE_NOT_FOUND = "previous_response_not_found"
TERMINAL_EVENTS = frozenset(
    {"response.completed", "response.failed", "response.incomplete", "error"}
)

#: Request properties that, when changed, force a full resend
#: (``responses_request_properties_mismatch`` in codex-rs, plus
#: ``instructions`` because Preloop may set them per request).
FINGERPRINT_KEYS: Tuple[str, ...] = (
    "model",
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "store",
    "stream",
    "include",
    "service_tier",
    "prompt_cache_key",
    "text",
)

#: Item fields that do not change what the model sees. Codex compares typed
#: items ignoring its internal passthrough metadata (``client.rs:452-470``):
#: the CLI stamps that onto items it got back (with a local ``create_time``)
#: and resends it, while the ChatGPT backend's own item-level ``metadata`` is
#: not a field of the CLI's typed item, so it is dropped before the resend
#: (observed live on gpt-6.1-sol, 2026-10-10). ids/status are bookkeeping.
_IGNORED_ITEM_KEYS = frozenset(
    {"id", "status", "metadata", "internal_chat_message_metadata_passthrough"}
)

#: Process-wide counters, read by :func:`metrics_snapshot`.
_METRICS: Counter = Counter()
_METRICS_LOCK = threading.Lock()


def count(name: str, amount: int = 1) -> None:
    """Increment a transport counter (never raises)."""
    with _METRICS_LOCK:
        _METRICS[name] += amount


def metrics_snapshot() -> Dict[str, int]:
    """Copy of the transport counters (incremental/full/fallback reasons/...)."""
    with _METRICS_LOCK:
        return dict(_METRICS)


def reset_metrics() -> None:
    with _METRICS_LOCK:
        _METRICS.clear()


def request_fingerprint(payload: Dict[str, Any]) -> Dict[str, str]:
    """Per-property digests of the fields that must not change mid-chain."""
    return {
        key: hashlib.sha256(
            json.dumps(
                payload.get(key), sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        ).hexdigest()[:16]
        for key in FINGERPRINT_KEYS
    }


def _canonical(value: Any, *, top: bool = False) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if top and key in _IGNORED_ITEM_KEYS:
                continue
            canon = _canonical(inner)
            if canon in (None, [], {}):
                continue
            out[key] = canon
        return out
    if isinstance(value, list):
        return [_canonical(inner) for inner in value]
    return value


def canonical_item(item: Any) -> str:
    """Comparison key for one input/output item (ignores ids/status/empties)."""
    return json.dumps(_canonical(item, top=True), sort_keys=True, default=str)


@dataclass
class IncrementalPlan:
    """Outcome of the diff rule for one call."""

    mode: str  # "incremental" | "full"
    reason: str  # "incremental" or the mismatch reason
    items: List[Any]
    previous_response_id: Optional[str] = None


def plan_request(
    entry: Optional["WsEntry"], payload: Dict[str, Any]
) -> IncrementalPlan:
    """Apply the Codex incremental rule to a post-hook upstream payload."""
    current_input = list(payload.get("input") or [])
    full = IncrementalPlan(mode="full", reason="", items=current_input)
    if entry is None or entry.last_input is None:
        full.reason = "no_previous_request"
        return full
    if entry.last_response_id is None or entry.last_output is None:
        full.reason = "no_previous_response"
        return full
    current_fingerprint = request_fingerprint(payload)
    if entry.fingerprint != current_fingerprint:
        full.reason = "properties_changed"
        logger.info(
            "Codex upstream WS full resend: reason=properties_changed keys=%s",
            sorted(
                key
                for key in FINGERPRINT_KEYS
                if (entry.fingerprint or {}).get(key) != current_fingerprint.get(key)
            ),
        )
        return full
    previous = [canonical_item(i) for i in entry.last_input] + [
        canonical_item(i) for i in entry.last_output
    ]
    if len(current_input) < len(previous):
        full.reason = "input_shortened"
        return full
    for index, expected in enumerate(previous):
        if canonical_item(current_input[index]) != expected:
            full.reason = "input_mismatch"
            _log_mismatch(index, json.loads(expected), current_input[index])
            return full
    return IncrementalPlan(
        mode="incremental",
        reason="incremental",
        items=current_input[len(previous) :],
        previous_response_id=entry.last_response_id,
    )


def _log_mismatch(index: int, expected: Any, current: Any) -> None:
    """Log where the chain diverged: item index, types and key names only."""
    current_canon = _canonical(current, top=True)
    if isinstance(expected, dict) and isinstance(current_canon, dict):
        keys = sorted(
            key
            for key in set(expected) | set(current_canon)
            if expected.get(key) != current_canon.get(key)
        )
        logger.info(
            "Codex upstream WS full resend: reason=input_mismatch index=%d "
            "expected_type=%s current_type=%s differing_keys=%s",
            index,
            expected.get("type"),
            current_canon.get("type"),
            keys,
        )
    else:
        logger.info(
            "Codex upstream WS full resend: reason=input_mismatch index=%d", index
        )


def build_frame(
    payload: Dict[str, Any], plan: IncrementalPlan, turn_state: Optional[str]
) -> Dict[str, Any]:
    """``response.create`` frame (codex-api ``common.rs`` 331-390)."""
    frame: Dict[str, Any] = {"type": "response.create"}
    for key, value in payload.items():
        if key in ("input", "previous_response_id", "type"):
            continue
        frame[key] = value
    if plan.previous_response_id:
        frame["previous_response_id"] = plan.previous_response_id
    frame["input"] = plan.items
    metadata = dict(payload.get("client_metadata") or {})
    if turn_state:
        # On WS the turn state travels in client_metadata, not as a header
        # (codex-rs client.rs:2032).
        metadata[TURN_STATE_KEY] = turn_state
    if metadata:
        frame["client_metadata"] = metadata
    return frame


class CodexWsHandshakeError(Exception):
    """The upgrade was refused or could not be completed."""

    def __init__(self, reason: str, status: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class CodexWsTransportError(Exception):
    """The socket failed or closed while a call was in flight."""


class CodexWsUpstreamError(Exception):
    """The upstream answered the frame with an ``error`` event."""

    def __init__(self, status: int, code: Optional[str], message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class WsEntry:
    """One warm upstream socket and the chain it carries."""

    key: Tuple[str, str]
    socket: Any
    auth_digest: str
    opened_at: float
    last_used: float
    fingerprint: Optional[Dict[str, str]] = None
    last_input: Optional[List[Any]] = None
    last_response_id: Optional[str] = None
    last_output: Optional[List[Any]] = None
    turn_state: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset_chain(self) -> None:
        self.fingerprint = None
        self.last_input = None
        self.last_response_id = None
        self.last_output = None

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            logger.debug("Closing Codex upstream socket failed", exc_info=True)


def auth_digest(token: str, account_id: str) -> str:
    """Digest of the upstream identity; never stored or logged in clear."""
    return hashlib.sha256(f"{account_id}\0{token}".encode("utf-8")).hexdigest()


def default_connect(
    headers: Dict[str, str], *, open_timeout: float
) -> Tuple[Any, Optional[str]]:
    """Open the upstream socket; returns (socket, handshake turn-state)."""
    from websockets.exceptions import InvalidStatus, WebSocketException
    from websockets.sync.client import connect

    user_agent = headers.pop("User-Agent", None)
    try:
        socket = connect(
            CODEX_WS_URL,
            additional_headers=headers,
            user_agent_header=user_agent,
            open_timeout=open_timeout,
            max_size=None,
            compression=None,
        )
    except InvalidStatus as exc:
        status = exc.response.status_code
        raise CodexWsHandshakeError(f"http_{status}", status=status) from exc
    except (WebSocketException, OSError, TimeoutError) as exc:
        raise CodexWsHandshakeError(type(exc).__name__) from exc
    turn_state = None
    response = getattr(socket, "response", None)
    if response is not None:
        turn_state = response.headers.get(TURN_STATE_KEY)
    return socket, turn_state


def iter_frame_events(
    socket: Any, frame: Dict[str, Any], *, timeout: float
) -> Iterator[Dict[str, Any]]:
    """Send one frame and yield decoded events up to the terminal one.

    Raises :class:`CodexWsUpstreamError` on an ``error`` event and
    :class:`CodexWsTransportError` when the socket fails mid-call.
    """
    from websockets.exceptions import WebSocketException

    deadline = time.monotonic() + timeout
    try:
        socket.send(json.dumps(frame))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexWsTransportError("timeout")
            raw = socket.recv(timeout=remaining)
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                logger.debug("Skipping unparseable Codex WS frame")
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "error":
                error = (
                    event.get("error") if isinstance(event.get("error"), dict) else {}
                )
                status = event.get("status") or event.get("status_code") or 502
                raise CodexWsUpstreamError(
                    int(status) if str(status).isdigit() else 502,
                    error.get("code"),
                    str(error.get("message") or "Codex upstream returned an error"),
                )
            yield event
            if kind in TERMINAL_EVENTS:
                return
    except (WebSocketException, OSError, TimeoutError) as exc:
        raise CodexWsTransportError(type(exc).__name__) from exc


class CodexWsRegistry:
    """Bounded per-pod LRU of warm upstream sockets."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        idle_timeout_s: float = 600.0,
        max_age_s: float = 55 * 60.0,
        http_only_ttl_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_entries = max_entries
        self.idle_timeout_s = idle_timeout_s
        self.max_age_s = max_age_s
        self.http_only_ttl_s = http_only_ttl_s
        self.clock = clock
        self._entries: "OrderedDict[Tuple[str, str], WsEntry]" = OrderedDict()
        self._http_only: Dict[Tuple[str, str], Tuple[float, str]] = {}
        self._lock = threading.Lock()

    def configure(self, **kwargs: Any) -> None:
        for name, value in kwargs.items():
            if value is not None:
                setattr(self, name, value)

    def http_only_reason(self, key: Tuple[str, str]) -> Optional[str]:
        with self._lock:
            mark = self._http_only.get(key)
            if mark is None:
                return None
            if mark[0] <= self.clock():
                self._http_only.pop(key, None)
                return None
            return mark[1]

    def mark_http_only(self, key: Tuple[str, str], reason: str) -> None:
        with self._lock:
            self._http_only[key] = (self.clock() + self.http_only_ttl_s, reason)
            if len(self._http_only) > self.max_entries * 4:
                now = self.clock()
                for stale in [k for k, v in self._http_only.items() if v[0] <= now]:
                    self._http_only.pop(stale, None)

    def expiry_reason(self, entry: WsEntry) -> Optional[str]:
        now = self.clock()
        if now - entry.opened_at >= self.max_age_s:
            return "age_cap"
        if now - entry.last_used >= self.idle_timeout_s:
            return "idle_timeout"
        return None

    def get(self, key: Tuple[str, str]) -> Optional[WsEntry]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, entry: WsEntry) -> None:
        evicted: List[WsEntry] = []
        with self._lock:
            old = self._entries.pop(entry.key, None)
            if old is not None and old is not entry:
                evicted.append(old)
            self._entries[entry.key] = entry
            while len(self._entries) > self.max_entries:
                _, oldest = self._entries.popitem(last=False)
                evicted.append(oldest)
                count("evicted_lru")
        for stale in evicted:
            stale.close()

    def drop(self, entry: WsEntry) -> None:
        with self._lock:
            if self._entries.get(entry.key) is entry:
                self._entries.pop(entry.key, None)
        entry.close()

    def sweep(self) -> int:
        """Close idle/aged entries that are not in use; returns how many."""
        expired: List[Tuple[WsEntry, str]] = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                reason = self.expiry_reason(entry)
                if reason and entry.lock.acquire(blocking=False):
                    try:
                        self._entries.pop(key, None)
                        expired.append((entry, reason))
                    finally:
                        entry.lock.release()
        for entry, reason in expired:
            count(f"fallback_{reason}")
            logger.info(
                "Codex upstream WS fallback: reason=%s action=close account=%s",
                reason,
                entry.key[0],
            )
            entry.close()
        return len(expired)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._http_only.clear()
        for entry in entries:
            entry.close()


#: The per-pod registry used by the gateway.
REGISTRY = CodexWsRegistry()
