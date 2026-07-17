"""Module 10 — onboard a custom agent through the API and run one short
session through the gateway, with the terminal activity recorded locally
(pexpect+agg).

The agent itself is scripts/e2e-rig/research_agent.py (stdlib-only). If the
account has no AI model configured yet, the gateway exposes no models and the
session is recorded as a SKIP — registration/credential minting must still
succeed for the module to pass/skip rather than fail.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import capture  # noqa: E402
import riglib  # noqa: E402

URL = riglib.env("RIG_URL").rstrip("/")
RIG = Path(__file__).resolve().parent.parent
CAST = riglib.run_dir() / "casts" / "10-custom-agent.cast"
MP4 = riglib.run_dir() / "casts" / "10-custom-agent.mp4"
AGENT_STATE = riglib.run_dir() / "state" / "custom-agent.json"


def main() -> None:
    creds = riglib.load_creds()
    token = riglib.user_token(URL, creds)
    python = riglib.env("RIG_PYTHON")

    CAST.parent.mkdir(parents=True, exist_ok=True)
    rec = capture.CastRecorder(CAST)
    child = capture.spawn_shell(rec)  # local shell, recorded
    exit_code = None
    try:
        # Token passed via env (exported off-screen would still echo; instead
        # export it in a hidden pre-step: type only the reference on screen).
        child.send(f"export PRELOOP_USER_TOKEN='{token}' HISTFILE=\r")
        child.expect_exact(capture.PROMPT.encode())
        capture.clear_buffer(child)
        rec.reset()  # the export never appears in the saved cast
        time.sleep(0.8)

        cmd = (
            f"{python} {RIG / 'research_agent.py'} --url {URL} "
            f"--name 'Research Agent (e2e)' --state-out {AGENT_STATE}"
        )
        capture.run_command(child, rec, cmd, mark="research_agent", timeout=300)

        # Recover the script's exit code without polluting the recording much.
        rec.mark("exit_code")
        capture.human_type(child, "echo rc=$?")
        child.send("\r")
        child.expect_exact(capture.PROMPT.encode(), timeout=15)
        time.sleep(1.5)
        rec.mark("end")

        import re

        buffer = b"".join(e[1].encode("utf-8", "replace") for e in rec.events[-50:])
        m = re.search(rb"rc=(\d+)", buffer)
        exit_code = int(m.group(1)) if m else None

        child.sendline("exit")
        child.close()
    finally:
        rec.compress_idle(cap=2.5)
        rec.save()
        capture.render(CAST, MP4)
        riglib.log(f"cast: {CAST}\nvideo: {MP4}")

    # The agent must exist server-side regardless of the session outcome.
    agents = riglib.list_agents(URL, token)
    match = [a for a in agents if a.get("display_name") == "Research Agent (e2e)"]
    if not match:
        raise SystemExit("custom agent not found in /api/v1/agents after onboarding")
    riglib.log(f"custom agent registered: {match[0].get('id')}")

    if exit_code == 0:
        riglib.note("custom agent onboarded and one session ran through the gateway")
    elif exit_code == 3:
        riglib.skip(
            "custom agent onboarded; gateway session SKIPPED — no AI model "
            "configured in the account (expected on a bare OSS install)"
        )
    else:
        raise SystemExit(f"research agent failed (exit {exit_code}) — see cast")


if __name__ == "__main__":
    main()
