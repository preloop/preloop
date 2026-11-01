"""Container test: an agent command cannot change the hosted Codex hook (#2142).

Runs the real launch block in the universal image as root, then tries to
overwrite or remove the hook CLI, ``hooks.json`` and ``config.toml`` as the
agent user, the way a shell command from Codex would. Every attempt must
fail with a permission error and leave the files unchanged, so later
commands are still checked by the same hook. Also checks that the agent user
cannot read the environment of a process that holds the runtime credential.

Then it plants git hooks and configuration in the workspace as the agent and
checks that root's git, after the workspace is taken back, runs none of it.

Needs Docker and a local image named by ``PRELOOP_UNIVERSAL_TEST_IMAGE``
(for example one built from environments/universal/Dockerfile). An image
without the ``preloop-agent`` user gets one added inside the throwaway
container, so the check also runs against an older image. The universal
image workflow runs this file against the image it builds:

    PRELOOP_UNIVERSAL_TEST_IMAGE=preloop-codex-universal:smoke \\
        python3 backend/tests/agents/test_codex_agent_user_container.py

The launch module is loaded from its file, so this needs neither the
backend's dependencies nor its conftest.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK_PY = (
    Path(__file__).resolve().parents[2] / "preloop" / "agents" / "codex_native_hook.py"
)
_spec = importlib.util.spec_from_file_location("_codex_native_hook", _HOOK_PY)
assert _spec is not None and _spec.loader is not None
_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hook)
AGENT_USER = _hook.AGENT_USER
codex_agent_user_block = _hook.codex_agent_user_block
codex_reclaim_workspace_block = _hook.codex_reclaim_workspace_block

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
mkdir -p /workspace/repo && cd /workspace/repo
git init -q . && git -c user.name=t -c user.email=t@example.com \\
    commit -q --allow-empty -m init
export PRELOOP_CHECKPOINT_PUT_TOKEN=put-secret
# A stand-in for the code-mode host Codex ships next to its binary.
_pl_pkg="$(npm root -g)/@openai/codex/vendor/fixture/bin"
mkdir -p "$_pl_pkg" && cp "$(readlink -f "$(command -v sleep)")" \\
    "$_pl_pkg/codex-code-mode-host"
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
PRELOOP_FLOW_TOKEN=flow-secret $PRELOOP_AS_AGENT \\
    "$_pl_pkg/codex-code-mode-host" 20 </dev/null &
sleep 2
pid="$(pgrep -u {AGENT_USER} -f codex-code-mode-host | head -1)"
if [ -n "$pid" ] && ! $PRELOOP_AS_AGENT cat "/proc/$pid/environ" </dev/null \\
        2>/dev/null | tr '\\0' '\\n' | grep -q flow-secret; then
    echo "RESULT code-mode host hidden"
fi
$PRELOOP_AS_AGENT bash -c 'env | grep -q put-secret' </dev/null \\
    || echo "RESULT upload token dropped"
# The agent plants git hooks and configuration for root's later git.
$PRELOOP_AS_AGENT bash -c '
    cd /workspace/repo
    printf "#!/bin/sh\\ntouch /tmp/pwned-hook\\n" > .git/hooks/post-commit
    chmod +x .git/hooks/post-commit
    mkdir -p /tmp/evil && printf "#!/bin/sh\\ntouch /tmp/pwned-config\\n" \\
        > /tmp/evil/post-commit && chmod +x /tmp/evil/post-commit
    git config core.hooksPath /tmp/evil
    nohup sleep 300 >/dev/null 2>&1 &
' </dev/null
{codex_reclaim_workspace_block()}
pgrep -u {AGENT_USER} >/dev/null || echo "RESULT agent processes stopped"
git -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m root
[ -e /tmp/pwned-hook ] || [ -e /tmp/pwned-config ] \\
    || echo "RESULT root git ran no agent hook"
[ "$(stat -c %u /workspace/repo/.git/config)" = 0 ] && echo "RESULT workspace root-owned"
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
    assert "code-mode host hidden" in results
    assert "upload token dropped" in results
    assert "agent processes stopped" in results
    assert "root git ran no agent hook" in results
    assert "workspace root-owned" in results


if __name__ == "__main__":
    if not _image_available():
        sys.exit("needs docker and PRELOOP_UNIVERSAL_TEST_IMAGE present locally")
    test_agent_commands_cannot_change_the_hook()
    print("agent user container test: OK")
