"""Recorded final offboard: `preloop agents offboard --all` on the VM in a
real pty, captured to an asciicast and rendered (pexpect+agg). Used by
module 12."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture  # noqa: E402
import riglib  # noqa: E402

HOST = riglib.env("RIG_HOST")
CAST = riglib.run_dir() / "casts" / "12-offboard.cast"
MP4 = riglib.run_dir() / "casts" / "12-offboard.mp4"


def main() -> None:
    CAST.parent.mkdir(parents=True, exist_ok=True)
    rec = capture.CastRecorder(CAST)
    child = capture.spawn_shell(rec, ssh_host=HOST)

    capture.run_command(
        child,
        rec,
        "preloop agents offboard --all --yes --remove-mcp-servers=no --remove-model=no",
        mark="offboard_all",
        timeout=600,
        answers={b"(Y/n):": "y", b"(y/N):": "y"},
    )
    time.sleep(1.0)
    capture.run_command(
        child, rec, "preloop agents list", mark="agents_list", timeout=120
    )
    time.sleep(2.0)
    rec.mark("end")
    child.sendline("exit")
    child.close()

    rec.compress_idle(cap=2.5)
    rec.save()
    capture.render(CAST, MP4)
    riglib.log(f"cast: {CAST}\nvideo: {MP4}")


if __name__ == "__main__":
    main()
