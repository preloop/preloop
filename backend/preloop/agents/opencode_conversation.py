"""Stdlib driver embedded in the hosted container, using OpenCode's live server.

The server owns the native session for its entire lifetime. Short-lived `run
--attach` clients submit turns and format events; they do not restart the agent
server. Control-plane messages and answers use an execution-scoped capability.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING or __package__:
    from .conversation_runtime import (
        ConversationRuntime,
        child_environment,
        control,
        drain,
        request,
    )
else:  # Standalone copy inside the hosted container.
    from conversation_runtime import (
        ConversationRuntime,
        child_environment,
        control,
        drain,
        request,
    )


def read_events(stream: Any, events: queue.Queue[dict[str, Any] | None]) -> None:
    """Read the pinned server SSE feed with its native event envelopes."""
    try:
        for line in stream:
            if line.startswith(b"data: "):
                events.put(json.loads(line[6:]))
    except (OSError, ValueError):
        pass
    finally:
        events.put(None)


class TurnText:
    """Assistant text for this turn, excluding user echoes and child sessions."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.user_message: str | None = None
        self.assistants: set[str] = set()
        self.parts: dict[str, str] = {}

    def accept(self, event: dict[str, Any]) -> None:
        """Apply a delta or authoritative snapshot without double appending."""
        properties = event.get("properties") or {}
        if properties.get("sessionID") != self.session_id:
            return
        kind = event.get("type")
        info = properties.get("info") or {}
        if kind == "message.updated":
            if info.get("role") == "user":
                self.user_message = info["id"]
            elif (
                info.get("role") == "assistant"
                and info.get("parentID") == self.user_message
            ):
                self.assistants.add(info["id"])
        part = properties.get("part") or {}
        if (
            kind == "message.part.updated"
            and part.get("messageID") in self.assistants
            and part.get("type") == "text"
        ):
            self.parts[part["id"]] = part.get("text", "")
        if (
            kind == "message.part.delta"
            and properties.get("messageID") in self.assistants
            and properties.get("field") == "text"
            and properties.get("partID") in self.parts
        ):
            self.parts[properties["partID"]] += properties.get("delta", "")

    @property
    def text(self) -> str:
        """Return text in the order its parts first appeared."""
        return "\n".join(self.parts.values())


def run_turn(
    server_url: str,
    model: str,
    prompt: str,
    turn: int,
    session_id: str,
    events: queue.Queue[dict[str, Any] | None],
) -> str:
    """Submit a turn to the existing server and durably publish answer snapshots."""
    command = [
        "opencode",
        "run",
        "--attach",
        server_url,
        "--format",
        "json",
        "--model",
        model,
    ]
    if session_id:
        command.extend(["--session", session_id])
    lines: queue.Queue[str | None] = queue.Queue()
    while not events.empty():
        if events.get_nowait() is None:
            raise RuntimeError("OpenCode event stream disconnected")
    streamed = TurnText(session_id)
    final_parts: dict[str, str] = {}
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=child_environment(),
    ) as process:
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        threading.Thread(target=drain, args=(process, lines), daemon=True).start()
        answer = ""
        failed = False
        interrupted = False
        completed = False
        last_poll = 0.0
        while True:
            while not events.empty():
                event = events.get_nowait()
                if event is None:
                    process.terminate()
                    raise RuntimeError("OpenCode event stream disconnected")
                streamed.accept(event)
            answer = streamed.text or "\n".join(final_parts.values())
            if time.monotonic() - last_poll >= 1:
                last_poll = time.monotonic()
                try:
                    control({"turn": turn, "answer": answer})
                    if control()["stop"]:
                        if session_id:
                            request(server_url + f"/session/{session_id}/abort", {})
                        process.terminate()
                        interrupted = True
                        break
                except (OSError, ValueError):
                    process.terminate()
                    raise
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            current_id = event.get("sessionID")
            if current_id and kind in {
                "step_start",
                "text",
                "step_finish",
                "error",
                "tool_use",
            }:
                if session_id and session_id != current_id:
                    continue
                session_id = current_id
                Path("/tmp/preloop-cli-session-id").write_text(session_id)
            if kind == "text":
                text = (event.get("part") or {}).get("text", "")
                final_parts[(event.get("part") or {})["id"]] = text
                print(text, flush=True)
            elif kind == "error":
                failed = True
            elif kind == "step_finish" and (event.get("part") or {}).get("reason") in {
                "stop",
                "end_turn",
            }:
                completed = True
                failed = False
        code = process.wait(timeout=30)
    answer = "\n".join(final_parts.values()) or streamed.text
    failed = not interrupted and (
        failed or code != 0 or not session_id or not completed
    )
    control(
        {
            "turn": turn,
            "answer": answer,
            "finished": True,
            "error": failed,
            "interrupted": interrupted,
        }
    )
    if failed:
        raise RuntimeError("OpenCode did not finish the conversation turn")
    print("PRELOOP_AGENT_SESSION opencode " + session_id, flush=True)
    return session_id


def main() -> int:
    """Run one live server until idle expiry, an explicit end or a limit."""
    model, prompt_path, session_id = sys.argv[1:]
    # The port is loopback-only within an isolated container, never published.
    server_url = "http://127.0.0.1:4096"
    server = subprocess.Popen(
        ["opencode", "serve", "--hostname", "127.0.0.1", "--port", "4096"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_environment(),
    )
    try:
        for _ in range(120):
            if server.poll() is not None:
                raise RuntimeError("OpenCode server exited during startup")
            try:
                request(server_url + "/global/health")
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("OpenCode server did not become ready")
        if session_id:
            try:
                restored = request(server_url + f"/session/{session_id}")
            except OSError as exc:
                raise RuntimeError(
                    "The saved OpenCode session could not be restored"
                ) from exc
            if restored.get("id") != session_id:
                raise RuntimeError(
                    "The restored OpenCode session identity does not match"
                )
        else:
            session_id = request(server_url + "/session", {})["id"]
        Path("/tmp/preloop-cli-session-id").write_text(session_id)
        print("PRELOOP_AGENT_SESSION opencode " + session_id, flush=True)
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        stream = urllib.request.urlopen(server_url + "/event", timeout=30)
        threading.Thread(target=read_events, args=(stream, events), daemon=True).start()

        def submit(prompt: str, turn: int) -> None:
            """Submit through the existing OpenCode transport."""
            nonlocal session_id
            session_id = run_turn(server_url, model, prompt, turn, session_id, events)

        ConversationRuntime(control).run(Path(prompt_path).read_text(), submit)
        print("FLOW_EXECUTION_SUCCESS", flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Exception type only: urllib exceptions can contain transport details.
        print("PRELOOP_CONVERSATION_FAILED " + type(exc).__name__, flush=True)
        return 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


if __name__ == "__main__":
    sys.exit(main())
