"""Harness-neutral hosted conversation transport and lifecycle.

This stdlib module is embedded next to each container driver. The capability
stays in the supervisor; same-UID process inspection is not an isolation boundary.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import time
import urllib.request
from collections.abc import Callable
from typing import Any


def request(
    url: str, body: dict[str, Any] | None = None, token: str | None = None
) -> Any:
    """Exchange one bounded JSON request without exposing credentials in logs."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read()
        return json.loads(data) if data else None


def child_environment() -> dict[str, str]:
    """Remove driver credentials from child environments, not same-UID /proc access."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PRELOOP_CONVERSATION_")
    }


def control(body: dict[str, Any] | None = None) -> Any:
    """Retry snapshots safely; failure closes the runtime instead of losing input."""
    for attempt in range(3):
        try:
            return request(
                os.environ["PRELOOP_CONVERSATION_URL"],
                body,
                os.environ["PRELOOP_CONVERSATION_TOKEN"],
            )
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1)
    raise RuntimeError("Conversation transport unavailable")


def drain(process: subprocess.Popen[str], lines: queue.Queue[str | None]) -> None:
    """Read CLI events without blocking lifecycle polling during a slow model call."""
    assert process.stdout is not None
    try:
        for line in process.stdout:
            lines.put(line)
    finally:
        lines.put(None)


class ConversationRuntime:
    """Drive admitted turns until the control plane requests a checkpoint/stop."""

    def __init__(self, transport: Callable[..., Any] = control) -> None:
        self.control = transport

    def run(self, prompt: str, submit: Callable[[str, int], None]) -> None:
        """Share lifecycle admission while each harness owns its native session."""
        if self.control()["stop"]:
            return
        turn = int(os.environ["PRELOOP_CONVERSATION_TURN"])
        submit(prompt, turn)
        while True:
            state = self.control()
            if state["stop"]:
                return
            if state.get("message") is not None and state["turn"] > turn:
                turn = state["turn"]
                submit(state["message"], turn)
            else:
                time.sleep(0.5)
