#!/usr/bin/env python3
"""Run one CLI command the way a person would and measure what they see.

The command runs in a pseudo-terminal of a fixed width (or with no terminal
at all). Each time it stops at a prompt, one answer is typed and counted as
an interaction. The result is printed as one JSON object: the transcript
with keys redacted, the number of interactions, the rows printed before the
apply decision and in total, the longest line, and the seconds between the
first prompt and the gateway probe result.

Standard library only: it is piped into the rig's agent container.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time

PROMPT_END = re.compile(r"[:?\]]\s*$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
REDACTIONS = (
    (re.compile(r"agt_[A-Za-z0-9_-]{8,}"), "agt_<redacted>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "sk-ant-<redacted>"),
    (re.compile(r"plr1\.[A-Za-z0-9_-]{16,}"), "plr1.<resume token>"),
)
DEFAULT_DECISION = r"Apply (managed Preloop onboarding for|to \d+ agents?)"
DEFAULT_PROBE = (
    r"probe passed|test request through the gateway|Gateway validation: passed"
)


def redact(text: str) -> str:
    """Replace every key-shaped string in text.

    Args:
        text: Raw terminal output.

    Returns:
        The text with agent keys, provider keys and resume tokens replaced.
    """
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def rows_at(text: str, columns: int) -> int:
    """Count the terminal rows text occupies when wrapped at columns.

    Args:
        text: Output without escape sequences.
        columns: Terminal width.

    Returns:
        Number of rows, counting an empty line as one row.
    """
    rows = 0
    for line in text.split("\n"):
        rows += max(1, -(-len(line) // columns))
    return rows


def parse_answers(pairs: list[str]) -> list[tuple[re.Pattern[str], str]]:
    """Parse REGEX=TEXT answer rules.

    Args:
        pairs: Rules as given on the command line.

    Returns:
        Compiled patterns with the text to type when one matches a prompt.

    Raises:
        SystemExit: If a rule has no "=".
    """
    rules = []
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--answer needs REGEX=TEXT, got {pair!r}")
        pattern, text = pair.split("=", 1)
        rules.append((re.compile(pattern), text))
    return rules


def run_in_terminal(args: argparse.Namespace) -> dict[str, object]:
    """Run the command in a pseudo-terminal, answering each prompt once.

    Args:
        args: Parsed command line.

    Returns:
        Raw output, the prompts answered, timings and the exit status.
    """
    import fcntl
    import pty
    import struct
    import termios

    rules = parse_answers(args.answer)
    pid, master = pty.fork()
    if pid == 0:
        os.execvp(args.command[0], args.command)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 50, args.columns, 0, 0))
    output = b""
    prompts: list[dict[str, object]] = []
    started = time.monotonic()
    last_data = started
    first_prompt_at = None
    probe_at = None
    probe = re.compile(args.probe)
    while True:
        now = time.monotonic()
        if now - started > args.timeout:
            os.kill(pid, 15)
            break
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            last_data = time.monotonic()
            if probe_at is None and probe.search(chunk.decode("utf-8", "replace")):
                probe_at = last_data
            continue
        text = ANSI.sub("", output.decode("utf-8", "replace")).replace("\r", "")
        last_line = text.rsplit("\n", 1)[-1]
        waiting = bool(last_line.strip()) and PROMPT_END.search(last_line)
        if not waiting or now - last_data < args.idle:
            continue
        if len(prompts) >= args.max_interactions:
            os.kill(pid, 15)
            break
        answer = args.default_answer
        for pattern, typed in rules:
            if pattern.search(last_line):
                answer = typed
                break
        if first_prompt_at is None:
            first_prompt_at = now
        prompts.append({"prompt": redact(last_line.strip()), "typed": answer})
        os.write(master, (answer + "\n").encode())
        last_data = time.monotonic()
    _, status = os.waitpid(pid, 0)
    seconds = None
    if first_prompt_at is not None and probe_at is not None:
        # The idle wait before each answer is the harness, not the product.
        seconds = round(probe_at - first_prompt_at - args.idle * (len(prompts) - 1), 2)
    return {
        "raw": output.decode("utf-8", "replace"),
        "prompts": prompts,
        "seconds_first_prompt_to_probe": seconds,
        "exit_status": os.waitstatus_to_exitcode(status),
    }


def run_without_terminal(args: argparse.Namespace) -> dict[str, object]:
    """Run the command with no terminal on any stream.

    Args:
        args: Parsed command line.

    Returns:
        Raw output and the exit status; no prompt can be answered.
    """
    completed = subprocess.run(
        args.command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=args.timeout,
        check=False,
    )
    return {
        "raw": completed.stdout.decode("utf-8", "replace"),
        "prompts": [],
        "seconds_first_prompt_to_probe": None,
        "exit_status": completed.returncode,
    }


def main() -> int:
    """Parse the command line, run the command and print the measurements.

    Returns:
        Process exit status (always 0; the command's own status is reported
        in the JSON).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--columns", type=int, default=80)
    parser.add_argument("--no-tty", action="store_true")
    parser.add_argument("--answer", action="append", default=[])
    parser.add_argument("--default-answer", default="")
    parser.add_argument("--decision", default=DEFAULT_DECISION)
    parser.add_argument("--probe", default=DEFAULT_PROBE)
    parser.add_argument("--idle", type=float, default=0.7)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--max-interactions", type=int, default=40)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")

    result = run_without_terminal(args) if args.no_tty else run_in_terminal(args)
    raw = str(result.pop("raw"))
    escapes = raw.count("\x1b")
    # Rows are counted on what was on screen; only the saved transcript has
    # its keys replaced.
    screen = ANSI.sub("", raw).replace("\r", "").rstrip("\n")
    text = redact(screen)
    decision = re.search(args.decision, screen)
    before = None
    if decision:
        before = rows_at(screen[: decision.start()].rstrip("\n"), args.columns)
    manual_rows = None
    manual = re.search(r"^Manual step (for|\d+ of)", screen, re.MULTILINE)
    if manual:
        end = re.search(r"^Press Enter when the change", screen[manual.start() :], re.MULTILINE)
        block = screen[manual.start() : manual.start() + end.start()] if end else screen[manual.start() :]
        manual_rows = rows_at(block.rstrip("\n"), args.columns)
    key_blocks = sum(1 for block in re.split(r"\n\s*\n", screen) if "agt_" in block)
    lines = text.split("\n")
    result.update(
        {
            "command": " ".join(args.command),
            "tty": not args.no_tty,
            "term": os.environ.get("TERM", ""),
            "columns": args.columns,
            "interactions": len(result["prompts"]),  # type: ignore[arg-type]
            "rows_before_decision": before,
            "rows_manual_step": manual_rows,
            "key_blocks": key_blocks,
            "rows_total": rows_at(screen, args.columns),
            "lines_total": len(lines),
            "longest_line": max((len(line) for line in lines), default=0),
            "escape_bytes": escapes,
            "transcript": text,
        }
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
