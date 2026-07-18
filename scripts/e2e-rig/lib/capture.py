"""Terminal capture: pexpect pty -> asciicast v2 -> agg gif -> ffmpeg mp4.

Adapted from media/videos/video4_see_them/record_terminal.py (the proven
pexpect+agg pipeline; VHS is never used — it stalls). The cast, the rendered
gif and the mp4 all share one timebase after idle compression.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pexpect

ROWS, COLS = 32, 132
FONT_SIZE = 24

# Preloop dark palette (DESIGN.md): navy surface, cyan cursor.
AGG_THEME = (
    "212632,E6EDF3,1a1e28,FF5D5D,26D962,F2A93B,0284C7,b18aff,30C9E8,E6EDF3,"
    "5c667a,FF5D5D,26D962,F2A93B,38bdf8,b18aff,30C9E8,ffffff"
)

PROMPT = "e2e> "


class CastRecorder:
    """Captures pty output into an asciicast v2 file with wall-clock timing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[tuple[float, str]] = []
        self.t0 = time.monotonic()
        self.marks: list[tuple[float, str]] = []

    def __call__(self, data: bytes) -> bytes:  # pexpect logfile_read interface
        self.events.append(
            (time.monotonic() - self.t0, data.decode("utf-8", "replace"))
        )
        return data

    def write(self, data: bytes) -> None:
        self(data)

    def flush(self) -> None:  # file protocol
        pass

    def mark(self, name: str) -> None:
        self.marks.append((time.monotonic() - self.t0, name))

    def reset(self) -> None:
        """Trim connection noise: restart the timeline at a clean prompt."""
        self.events = [(0.0, "\x1b[2J\x1b[H" + PROMPT)]
        self.t0 = time.monotonic()

    def compress_idle(self, cap: float = 2.5) -> None:
        """Cap gaps between output events; remap marks onto the new timebase."""
        new_events: list[tuple[float, str]] = []
        new_marks: list[tuple[float, str]] = []
        offset = 0.0
        prev = 0.0
        mi = 0
        marks = sorted(self.marks)
        for ts, data in self.events:
            gap = ts - prev
            if gap > cap:
                offset += gap - cap
            while mi < len(marks) and marks[mi][0] <= ts:
                new_marks.append((max(0.0, marks[mi][0] - offset), marks[mi][1]))
                mi += 1
            new_events.append((ts - offset, data))
            prev = ts
        while mi < len(marks):
            new_marks.append((max(0.0, marks[mi][0] - offset), marks[mi][1]))
            mi += 1
        self.events = new_events
        self.marks = new_marks

    def save(self) -> None:
        header = {
            "version": 2,
            "width": COLS,
            "height": ROWS,
            "idle_time_limit": 1000,
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        }
        with self.path.open("w") as fh:
            fh.write(json.dumps(header) + "\n")
            for ts, data in self.events:
                fh.write(json.dumps([round(ts, 4), "o", data]) + "\n")


def drain(child: pexpect.spawn) -> None:
    """Pull pending pty output NOW so cast timestamps reflect arrival time."""
    try:
        while True:
            if not child.read_nonblocking(4096, timeout=0):
                break
    except pexpect.TIMEOUT:
        pass  # Non-blocking drain: no output available (expected)
    except pexpect.EOF:
        pass  # Child process ended (caller handles)


def human_type(child: pexpect.spawn, text: str, cps: float = 24.0) -> None:
    """Type text character by character at a human-ish cadence."""
    for ch in text:
        child.send(ch)
        time.sleep(1.0 / cps)
        drain(child)


def spawn_shell(rec: CastRecorder, ssh_host: str | None = None) -> pexpect.spawn:
    """Spawn a recorded bash shell, locally or over ssh, with a fixed prompt."""
    bootstrap = (
        f"export PS1='{PROMPT}' PS2='' TERM=xterm-256color; "
        "export PATH=$HOME/.local/bin:$HOME/.opencode/bin:$PATH; "
        # Off-camera telemetry opt-out: every CLI command recorded in this
        # shell must stay out of adoption data (rig standing rule).
        "export PRELOOP_DISABLE_TELEMETRY=true; "
        "exec bash --norc --noprofile"
    )
    if ssh_host:
        child = pexpect.spawn(
            "ssh",
            ["-t", "-o", "BatchMode=yes", ssh_host, bootstrap],
            encoding=None,
            timeout=300,
            dimensions=(ROWS, COLS),
        )
    else:
        child = pexpect.spawn(
            "bash",
            ["-c", bootstrap],
            encoding=None,
            timeout=300,
            dimensions=(ROWS, COLS),
        )
    child.logfile_read = rec
    child.expect_exact(PROMPT.encode())
    # bash re-prints the prompt after PS1 export under some ssh configs;
    # settle briefly, then start the timeline clean.
    time.sleep(0.5)
    drain(child)
    rec.reset()
    time.sleep(0.8)
    return child


def clear_buffer(child: pexpect.spawn) -> None:
    """Drop any unconsumed pty output (e.g. an extra prompt) so the next
    expect cannot match stale content and return early."""
    drain(child)
    try:
        child._buffer.truncate(0)
        child._buffer.seek(0)
    except AttributeError:
        # Older pexpect versions may not expose an internal buffer to clear.
        pass


def run_command(
    child: pexpect.spawn,
    rec: CastRecorder,
    command: str,
    mark: str,
    timeout: int = 300,
    answers: dict[bytes, str] | None = None,
) -> None:
    """Type a command, then wait for the prompt, answering interactive
    prompts along the way (pattern -> reply). Raises on timeout."""
    clear_buffer(child)
    rec.mark(mark)
    human_type(child, command)
    time.sleep(0.4)
    child.send("\r")

    patterns: list[bytes] = [PROMPT.encode()]
    replies: list[str] = []
    for pat, reply in (answers or {}).items():
        patterns.append(pat)
        replies.append(reply)

    deadline = time.monotonic() + timeout
    while True:
        remaining = max(1, int(deadline - time.monotonic()))
        idx = child.expect_exact(patterns, timeout=remaining)
        if idx == 0:
            break
        time.sleep(0.8)
        reply = replies[idx - 1]
        if reply:
            human_type(child, reply)
        child.send("\r")


def render(cast: Path, out_mp4: Path) -> None:
    """Render the cast to gif (agg) and mp4 (ffmpeg), 1080p padded."""
    gif = cast.with_suffix(".gif")
    subprocess.run(
        [
            "agg",
            "--font-size",
            str(FONT_SIZE),
            "--fps-cap",
            "30",
            "--theme",
            AGG_THEME,
            "--line-height",
            "1.4",
            "--idle-time-limit",
            "1000",
            str(cast),
            str(gif),
        ],
        check=True,
    )
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg required")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(gif),
            "-vf",
            (
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                + "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#212632,fps=30"
            ),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            str(out_mp4),
        ],
        check=True,
        capture_output=True,
    )
