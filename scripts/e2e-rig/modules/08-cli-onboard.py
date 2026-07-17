"""Module 08 — install the pinned CLI on the VM, log in against the fresh
instance, onboard all discovered agents. Every keystroke runs in a real pty
over ssh and is recorded to an asciicast (pexpect+agg pipeline, never vhs).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import capture  # noqa: E402
import riglib  # noqa: E402

HOST = riglib.env("RIG_HOST")
URL = riglib.env("RIG_URL").rstrip("/")
CLI_VERSION = riglib.env("RIG_CLI_VERSION")

CAST = riglib.run_dir() / "casts" / "08-cli-onboard.cast"
MP4 = riglib.run_dir() / "casts" / "08-cli-onboard.mp4"
# The CLI's own login persistence target (see cli/internal/config/config.go).
CLI_CONFIG_FILE = "$HOME/.preloop/config.yaml"


def ssh(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOST, cmd],
        capture_output=True,
        text=True,
        check=check,
    )


def stage_cli_login(token: str) -> None:
    """Persist the CLI login on the VM without the token ever appearing in
    any argv, environment, recording, or `ps` output.

    `preloop login --token <t>` would put the token on the preloop process's
    command line (visible in the VM's process listing while it runs), and the
    released CLI has no stdin/env path for token login. Its token login only
    writes ~/.preloop/config.yaml (config.SetTokens), so write that file
    directly: the token travels exclusively over the ssh stdin stream into
    `install -m 600 /dev/stdin` — the remote command string contains no
    secret. The recorded scene then runs `preloop auth status`, which
    verifies the stored login against the server on camera.
    """
    config_yaml = (
        f"access_token: {json.dumps(token)}\n"
        f'refresh_token: ""\n'
        f"api_url: {json.dumps(URL)}\n"
    )
    subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            HOST,
            "umask 077; mkdir -p ~/.preloop && "
            f'install -m 600 /dev/stdin "{CLI_CONFIG_FILE}"',
        ],
        input=config_yaml,
        text=True,
        check=True,
    )


def main() -> None:
    creds = riglib.load_creds()
    if not creds.get("api_token"):
        raise SystemExit("no api_token in creds.json — run module 07 first")
    # Tokens expire (60 min); mint a fresh one so a slow run cannot go stale.
    token = riglib.user_token(URL, creds)
    stage_cli_login(token)

    CAST.parent.mkdir(parents=True, exist_ok=True)
    rec = capture.CastRecorder(CAST)
    child = capture.spawn_shell(rec, ssh_host=HOST)
    try:
        installer = (
            f"curl -fsSL https://github.com/preloop/preloop/releases/download/"
            f"v{CLI_VERSION}/install-cli.sh -o /tmp/install-cli.sh && "
            f"PRELOOP_VERSION={CLI_VERSION} PRELOOP_URL={URL} sh /tmp/install-cli.sh"
        )
        # The installer prompts to sign in; we decline — the login was staged
        # out-of-band (see stage_cli_login) so no secret enters the recording.
        capture.run_command(
            child,
            rec,
            installer,
            mark="install_cli",
            timeout=300,
            answers={
                b"[Y/s/n]": "n",
                b"Onboard discovered agents now?": "n",
            },
        )
        time.sleep(1.0)

        # Verify the staged login on camera (server round-trip, no secrets).
        capture.run_command(
            child,
            rec,
            "preloop auth status",
            mark="auth_status",
            timeout=60,
        )
        time.sleep(1.0)

        # Onboard everything discover finds; --yes auto-approves, but keep
        # answers for any prompt --yes does not cover (agent name, re-onboard).
        capture.run_command(
            child,
            rec,
            "preloop agents discover --yes",
            mark="onboard_all",
            timeout=900,
            answers={
                b"Agent name": "",
                b"(Y/n):": "y",
                b"(y/N):": "y",
            },
        )
        time.sleep(1.0)

        capture.run_command(
            child, rec, "preloop agents list", mark="agents_list", timeout=120
        )
        time.sleep(2.0)
        rec.mark("end")
        child.sendline("exit")
    finally:
        child.close()

    rec.compress_idle(cap=2.5)
    rec.save()
    capture.render(CAST, MP4)
    riglib.log(f"cast: {CAST}\nvideo: {MP4}")

    # Structured post-check (not recorded): what actually got onboarded.
    result = ssh("preloop agents list --json 2>/dev/null || true", check=False)
    agents = []
    try:
        parsed = json.loads(result.stdout or "[]")
        agents = parsed if isinstance(parsed, list) else parsed.get("agents", [])
    except json.JSONDecodeError:
        riglib.note("agents list --json unavailable/unparseable; saving raw text")
        (riglib.run_dir() / "state" / "onboarded-agents.txt").write_text(result.stdout)
    riglib.save_state("onboarded-agents.json", agents)

    names = [a.get("display_name") for a in agents if isinstance(a, dict)]
    if not names:
        raise SystemExit("no agents onboarded — check the cast/log")
    riglib.note(f"onboarded agents: {', '.join(str(n) for n in names)}")


if __name__ == "__main__":
    main()
