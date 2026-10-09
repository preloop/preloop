"""Drive Claude Code's interactive /login through a pseudo-terminal.

Test-only. Starts `claude` in a PTY (HOME is the caller's throwaway HOME),
types /login, scrapes the device user code from the screen, and prints it on
stdout so the caller can complete the browser leg. Then waits until Claude
Code reports a successful sign-in, or times out.
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time

CODE_RE = re.compile(
    r"user_code=([A-Z0-9]{4}-[A-Z0-9]{4})|\b([A-Z0-9]{4}-[A-Z0-9]{4})\b"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def main() -> int:
    """Run the PTY session."""
    code_file = sys.argv[1]
    done_file = sys.argv[2]
    master, slave = pty.openpty()
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="200", LINES="50")
    proc = subprocess.Popen(
        ["claude"], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
    )
    os.close(slave)
    screen = ""
    sent_login = False
    code_written = False
    deadline = time.time() + float(os.environ.get("TUI_TIMEOUT", "180"))
    last_enter = 0.0
    while time.time() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                data = os.read(master, 65536).decode("utf-8", "replace")
            except OSError:
                break
            screen += ANSI_RE.sub("", data)
            screen = screen[-20000:]
        flat = re.sub(r"\s+", " ", screen)
        if (
            not sent_login
            and time.time() > deadline - float(os.environ.get("TUI_TIMEOUT", "180")) + 4
        ):
            # Accept trust/onboarding prompts with Enter, then type /login.
            os.write(master, b"\r")
            time.sleep(1)
            os.write(master, b"/login")
            time.sleep(0.5)
            os.write(master, b"\r")
            sent_login = True
        if sent_login and not code_written:
            match = CODE_RE.search(flat)
            if match:
                code = match.group(1) or match.group(2)
                with open(code_file, "w") as fh:
                    fh.write(code)
                code_written = True
            elif time.time() - last_enter > 3:
                tail = re.sub(r"\s+", "", screen[-1500:])
                if "Trustgateway" in tail and "❯No," in tail:
                    # First contact: the cursor sits on "No, go back".
                    os.write(master, b"\x1b[A")
                    time.sleep(0.5)
                os.write(master, b"\r")
                last_enter = time.time()
        if code_written and os.path.exists(done_file):
            if re.search(
                r"(?i)login successful|signed in|logged in|Connected ?to ?Cloud ?gateway",
                flat,
            ):
                print("TUI_LOGIN_OK", flush=True)
                proc.terminate()
                return 0
    sys.stderr.write(screen[-3000:])
    proc.terminate()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
