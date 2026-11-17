"""Native context carrier, confirmed by Hermes' admitted API request observer."""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


def contains(value: Any, text: str) -> bool:
    """Inspect strings without serializing private conversation content."""
    if isinstance(value, str):
        return text in value
    if isinstance(value, dict):
        return any(contains(item, text) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains(item, text) for item in value)
    return False


@dataclass
class Claim:
    """One deferred batch owned by a native turn until admission."""

    text: str
    ids: list[str]
    turn: str
    confirmed: bool = False


@dataclass
class Session:
    """Serialize callbacks for one native conversation."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    registered: bool = False
    claim: Claim | None = None
    active: int = 0
    closed: bool = False


class OperatorNotes:
    """Claim at pre_llm_call and ACK only an admitted pre_api_request body."""

    def __init__(
        self, base: str, token: str, transport: Transport | None = None
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.transport = transport or self._post
        self.sessions: dict[str, Session] = {}
        self.ended_sessions: dict[str, None] = {}
        self.lock = threading.Lock()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base}/api/v1/agents/{path}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read(1 << 20))
        if not isinstance(result, dict):
            raise ValueError("Invalid note response")
        return result

    @contextmanager
    def _state(
        self, session_id: str, *, create: bool = False
    ) -> Iterator[Session | None]:
        """Pin callback state before waiting on its lock, then release atomically."""
        with self.lock:
            state = self.sessions.get(session_id)
            if state is None and create:
                if len(self.sessions) >= 256:
                    idle = next(
                        (
                            key
                            for key, item in self.sessions.items()
                            if not item.active and item.claim is None
                        ),
                        None,
                    )
                    if idle is not None:
                        del self.sessions[idle]
                if len(self.sessions) < 256:
                    state = Session()
                    self.sessions[session_id] = state
            if state is not None:
                state.active += 1
        if state is None:
            yield None
            return
        try:
            with state.lock:
                yield None if state.closed else state
        finally:
            with self.lock:
                state.active -= 1
                if (
                    state.closed
                    and not state.active
                    and self.sessions.get(session_id) is state
                ):
                    del self.sessions[session_id]

    def _ack(self, session_id: str, state: Session) -> None:
        claim = state.claim
        if claim is None or not claim.confirmed:
            return
        self.transport("notes/ack", {"session_id": session_id, "note_ids": claim.ids})
        state.claim = None

    def prepare(self, session_id: str, turn_id: str) -> dict[str, str] | None:
        """Return supported context output without advancing its receipt."""
        if not session_id or len(session_id) > 200 or not turn_id:
            return None
        with self._state(session_id, create=True) as state:
            if state is None:
                return None
            try:
                if not state.registered:
                    result = self.transport(
                        "session-start", {"session_id": session_id, "source": "hermes"}
                    )
                    if (
                        not isinstance(result.get("runtime_session_id"), str)
                        or not result["runtime_session_id"]
                    ):
                        return None
                    state.registered = True
                self._ack(session_id, state)
                if state.claim and state.claim.turn not in ("", turn_id):
                    return None
                if state.claim is None:
                    response = self.transport(
                        "notes/pending",
                        {
                            "session_id": session_id,
                            "channel": "hook",
                            "event": "pre_tool_use",
                            "defer_delivery": True,
                        },
                    )
                    notes = response.get("notes")
                    ids = (
                        [
                            item["note_id"]
                            for item in notes
                            if isinstance(item, dict)
                            and isinstance(item.get("note_id"), str)
                        ]
                        if isinstance(notes, list)
                        else []
                    )
                    text = response.get("text")
                    if not isinstance(text, str) or not text or not ids:
                        return None
                    state.claim = Claim(text, list(dict.fromkeys(ids)), turn_id)
                state.claim.turn = turn_id
                return {"context": state.claim.text}
            except Exception:
                return None  # Notes fail open; native approvals remain independent.

    def admitted(self, session_id: str, turn_id: str, messages: Any) -> None:
        """ACK only the batch confirmed in this turn's effective request body."""
        with self._state(session_id) as state:
            if state is None:
                return
            claim = state.claim
            if (
                claim is None
                or claim.turn != turn_id
                or not contains(messages, claim.text)
            ):
                return
            claim.confirmed = True
            try:
                self._ack(session_id, state)
            except Exception:
                pass  # Retry before any later claim, without injecting again.

    def turn_ended(self, session_id: str, turn_id: str) -> None:
        """Release unadmitted context after its own turn; never end a session."""
        with self._state(session_id) as state:
            if state is None:
                return
            if (
                state.claim
                and state.claim.turn == turn_id
                and not state.claim.confirmed
            ):
                state.claim.turn = ""

    def finalized(self, session_id: str, reason: str) -> None:
        """Only Hermes' authoritative on_session_finalize closes the run."""
        if not session_id or len(session_id) > 200:
            return
        with self._state(session_id, create=True) as state:
            with self.lock:
                if session_id in self.ended_sessions:
                    return
            try:
                if state is not None:
                    self._ack(session_id, state)
                self.transport(
                    "session-end", {"external_session_id": session_id, "reason": reason}
                )
            except Exception:
                return  # TTL or another authority may terminalize an unreachable run.
            with self.lock:
                self.ended_sessions[session_id] = None
                if len(self.ended_sessions) > 256:
                    self.ended_sessions.pop(next(iter(self.ended_sessions)))
            if state is not None:
                state.closed = True
