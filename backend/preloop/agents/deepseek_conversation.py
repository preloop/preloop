"""Supervise the persistent DeepSeek core driver using the shared runtime API."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from .conversation_runtime import (
        ConversationRuntime,
        child_environment,
        control,
        drain,
    )
else:  # Standalone copy inside the hosted container.
    from conversation_runtime import (
        ConversationRuntime,
        child_environment,
        control,
        drain,
    )


class DeepSeekRuntime(ConversationRuntime):
    """Keep one dsh process and its native agent alive across admitted turns."""

    def __init__(self, session_id: str) -> None:
        super().__init__(control)
        env = child_environment()
        env["PRELOOP_DSH_SESSION"] = session_id
        env["PRELOOP_DSH_EXECUTABLE"] = shutil.which("dsh") or "dsh"
        self.process = subprocess.Popen(
            ["dsh", "--profile", "headless"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Package diagnostics may contain credentials; expose failure only.
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(
            target=drain, args=(self.process, self.lines), daemon=True
        ).start()
        self.session_id = session_id

    def event(self, timeout: float = 0.2) -> dict[str, Any] | None:
        """Read only driver frames; process exit and explicit errors fail visibly."""
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            raise RuntimeError("DeepSeek runtime disconnected")
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict) or event.get("preloop") is not True:
            return None
        if event.get("error"):
            raise RuntimeError("DeepSeek native driver failed")
        return event

    def ready(self) -> None:
        """Require an explicit matching identity before submitting any input."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            event = self.event()
            if event and event.get("ready"):
                sid = event["session"]
                if self.session_id and sid != self.session_id:
                    raise RuntimeError("DeepSeek restored a different session")
                self.session_id = sid
                Path("/tmp/preloop-cli-session-id").write_text(sid)
                print("PRELOOP_AGENT_SESSION deepseek " + sid, flush=True)
                return
        raise RuntimeError("DeepSeek startup timed out")

    def send(self, body: dict[str, Any]) -> None:
        """Write one framed command to the already-running native driver."""
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(body) + "\n")
        self.process.stdin.flush()

    def submit(self, prompt: str, turn: int) -> None:
        """Persist streamed answer snapshots and poll cancellation during work."""
        self.send({"prompt": prompt, "turn": turn})
        answer = ""
        last_poll = 0.0
        interrupted = False
        cancel_deadline = float("inf")
        while time.monotonic() < cancel_deadline:
            if time.monotonic() - last_poll >= 1:
                last_poll = time.monotonic()
                self.control({"turn": turn, "answer": answer})
                if not interrupted and self.control()["stop"]:
                    interrupted = True
                    cancel_deadline = time.monotonic() + 30
                    self.send({"cancel": True})
            event = self.event()
            if not event or event.get("turn") != turn:
                continue
            if event.get("delta") and not answer:
                print(f"PRELOOP_CONVERSATION_FIRST_TOKEN {turn}", flush=True)
            answer += event.get("delta", "")
            if event.get("finished"):
                answer = event["answer"] or answer
                failed = not interrupted and event.get("reason") != "completed"
                self.control(
                    {
                        "turn": turn,
                        "answer": answer,
                        "finished": True,
                        "error": failed,
                        "interrupted": interrupted,
                    }
                )
                if failed:
                    raise RuntimeError("DeepSeek did not finish the turn")
                print(answer, flush=True)
                return
        raise RuntimeError("DeepSeek cancellation timed out")

    def close(self) -> None:
        """Drain native persistence before shell checkpoint capture."""
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            code = self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("DeepSeek shutdown timed out") from None
        if code:
            raise RuntimeError("DeepSeek runtime exited unsuccessfully")


def main() -> int:
    """Run until idle expiry or a limit, flushing before native checkpointing."""
    prompt_path, session_id = sys.argv[1:]
    runtime = DeepSeekRuntime(session_id)
    try:
        runtime.ready()
        runtime.run(Path(prompt_path).read_text(), runtime.submit)
        runtime.close()
        print("FLOW_EXECUTION_SUCCESS", flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("PRELOOP_CONVERSATION_FAILED " + type(exc).__name__, flush=True)
        return 1
    finally:
        if runtime.process.poll() is None:
            runtime.process.kill()
            runtime.process.wait()


if __name__ == "__main__":
    sys.exit(main())
