"""Container test: an agent command cannot change the hosted Codex hook (#2142).

Runs the real launch block in the universal image as root, then tries to
overwrite or remove the hook CLI, ``hooks.json`` and ``config.toml`` as the
agent user, the way a shell command from Codex would. Every attempt must
fail with a permission error and leave the files unchanged, so later
commands are still checked by the same hook. Also checks that the agent user
cannot read the environment of a process that holds the runtime credential.

Needs Docker and a local image named by ``PRELOOP_UNIVERSAL_TEST_IMAGE``
(for example one built from environments/universal/Dockerfile). An image
without the ``preloop-agent`` user gets one added inside the throwaway
container, so the check also runs against an older image.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from preloop.agents.codex_native_hook import AGENT_USER, codex_agent_user_block

IMAGE = os.environ.get("PRELOOP_UNIVERSAL_TEST_IMAGE", "")


def _image_available() -> bool:
    if not IMAGE or shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _image_available(),
    reason="needs docker and PRELOOP_UNIVERSAL_TEST_IMAGE present locally",
)

ATTACKS = (
    ("overwrite cli", 'printf x > "$CLI"'),
    ("replace cli", 'cp /bin/true "$CLI"'),
    ("remove cli", 'rm -f "$CLI"'),
    ("overwrite hooks", "printf '{}' > \"$CODEX_HOME/hooks.json\""),
    ("remove hooks", 'rm -f "$CODEX_HOME/hooks.json"'),
    ("rename hooks", 'mv "$CODEX_HOME/hooks.json" "$CODEX_HOME/hooks.old"'),
    ("append config", "printf '[hooks]\\n' >> \"$CODEX_HOME/config.toml\""),
    ("rename config", 'mv "$CODEX_HOME/config.toml" /tmp/config.toml'),
    ("chmod config", 'chmod 0666 "$CODEX_HOME/config.toml"'),
)


def _script() -> str:
    attacks = "\n".join(
        f"attack {name.replace(' ', '_')} {_sq(cmd)}" for name, cmd in ATTACKS
    )
    return f"""
set -e
id -u {AGENT_USER} >/dev/null 2>&1 \\
    || useradd --uid 10001 --user-group --create-home {AGENT_USER}
mkdir -p /workspace && cd /workspace
export CODEX_HOME=/root/.codex
mkdir -p "$CODEX_HOME"
printf 'model = "gpt-5.4"\\n[hooks.state."k"]\\ntrusted_hash = "sha256:x"\\n' \\
    > "$CODEX_HOME/config.toml"
printf '{{"hooks": {{}}}}\\n' > "$CODEX_HOME/hooks.json"
printf '{{}}\\n' > "$CODEX_HOME/auth.json"
_pl_cli=/opt/preloop/bin/preloop
if [ ! -x "$_pl_cli" ]; then
    mkdir -p /opt/preloop/bin && cp /bin/true "$_pl_cli"
fi
{codex_agent_user_block()}
[ -n "$PRELOOP_AS_AGENT" ] || {{ echo "RESULT no-drop"; exit 1; }}
export CLI="$_pl_cli"
sums() {{ sha256sum "$CLI" "$CODEX_HOME/hooks.json" "$CODEX_HOME/config.toml"; }}
before="$(sums)"
attack() {{
    if $PRELOOP_AS_AGENT bash -c "$2" </dev/null 2>/tmp/attack.err; then
        echo "RESULT $1 allowed"
    elif grep -qE 'Permission denied|Operation not permitted' /tmp/attack.err; then
        echo "RESULT $1 denied"
    else
        echo "RESULT $1 failed-otherwise $(head -c 200 /tmp/attack.err)"
    fi
}}
{attacks}
[ "$before" = "$(sums)" ] && echo "RESULT files unchanged" || echo "RESULT files changed"
$PRELOOP_AS_AGENT bash -c 'touch /workspace/built "$CODEX_HOME/sessions/s" \\
    && echo ok > "$HOME/.cache-probe"' </dev/null && echo "RESULT workspace writable"
PRELOOP_FLOW_TOKEN=flow-secret $PRELOOP_AS_AGENT \\
    node -e 'setTimeout(() => {{}}, 20000)' </dev/null &
holder=$!
sleep 3
pid="$(pgrep -u {AGENT_USER} -x node | head -1)"
if [ -z "$pid" ]; then
    echo "RESULT no holder process"
elif $PRELOOP_AS_AGENT cat "/proc/$pid/environ" </dev/null 2>/dev/null \\
        | tr '\\0' '\\n' | grep -q flow-secret; then
    echo "RESULT environ readable"
else
    echo "RESULT environ hidden"
fi
kill "$holder" 2>/dev/null || true
"""


def _sq(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def test_agent_commands_cannot_change_the_hook():
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "-e",
            "CODEX_ENV_NODE_VERSION=20",
            "--entrypoint",
            "/opt/entrypoint.sh",
            IMAGE,
            "-s",
        ],
        input=_script(),
        capture_output=True,
        text=True,
        timeout=600,
    )
    results = [
        line.removeprefix("RESULT ")
        for line in proc.stdout.splitlines()
        if line.startswith("RESULT ")
    ]
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    for name, _ in ATTACKS:
        assert f"{name.replace(' ', '_')} denied" in results, results
    assert "files unchanged" in results
    assert "workspace writable" in results
    assert "environ hidden" in results
