"""The Preloop policy hook inside hosted Codex containers.

A private-runner host profile runs Codex with the Preloop ``PreToolUse``
hook, so native shell commands are checked against the account's tool
rules: deny rules block, and an approval-required rule parks the unattended
run (``preloop.services.native_flow_park``). Hosted containers ran Codex
under ``--yolo`` with no hook, so #2137 refused such launches with
``approval_unavailable``.

This block gives the hosted run the same gate. Before Codex starts it:

1. finds the Preloop CLI (``/opt/preloop/bin/preloop`` in the hosted image,
   else ``preloop`` on PATH, else a pinned release verified by SHA-256);
2. writes ``$CODEX_HOME/hooks.json`` with the ``PreToolUse`` hook in
   ``--require-flow-credential`` mode, which reads only the execution's
   runtime credential (``PRELOOP_FLOW_TOKEN``) and fails closed;
3. trusts that hook the way Codex itself does: ``codex app-server``
   ``hooks/list`` reports the hook's ``currentHash`` and the block records it
   under ``[hooks.state]`` in the container's own ``config.toml`` (the
   container is single-use and its configuration is ours to write);
4. re-reads ``hooks/list`` and requires the hook to be enabled and trusted.

The read-only sandbox is untouched: it disables the shell tool, so it has no
native commands to gate and no hook is installed there.

When any native deny or approval-required rule applies to the flow, a hook
that could not be installed fails the run before Codex starts with
``approval_unavailable`` instead of running those commands ungated. With no
native rule a missing hook is logged and the run continues as before.
"""

from __future__ import annotations

import shlex

#: Pinned Preloop CLI release used when the image does not ship the CLI.
#: 0.17.1 has ``permission-hook --require-flow-credential`` and
#: ``--hook-event PreToolUse``.
HOOK_CLI_VERSION = "0.17.1"
HOOK_CLI_SHA256 = {
    "amd64": "1d1317990a4111578f3f3a9064cee80ec2c79007abc186ca9db8be662829aac6",
    "arm64": "a2abb797413260e9253484c99e4ca3d93d0cf433e949357f21e28134cd5361b0",
}
#: Where the hosted image installs the CLI (environments/universal/Dockerfile).
IMAGE_CLI_PATH = "/opt/preloop/bin/preloop"

#: Log line printed once the hook is installed and trusted.
HOOK_READY_LINE = "PRELOOP_NATIVE_HOOK ready"

_TRUST_SCRIPT = r"""
import json, os, subprocess, sys, threading
cmd = sys.argv[1]
home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
cwd = os.getcwd()
def hooks():
    p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=cwd)
    timer = threading.Timer(30, p.kill); timer.start()
    try:
        def send(m):
            p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
        send({"id": 1, "method": "initialize", "params": {"clientInfo":
            {"name": "preloop-hosted-hook", "version": "1"},
            "capabilities": {"experimentalApi": True}}})
        for line in p.stdout:
            m = json.loads(line)
            if m.get("id") == 1:
                send({"method": "initialized"})
                send({"id": 2, "method": "hooks/list", "params": {"cwds": [cwd]}})
            elif m.get("id") == 2:
                if m.get("error"):
                    return []
                data = (m.get("result") or {}).get("data") or []
                return [h for d in data for h in (d.get("hooks") or [])]
        return []
    finally:
        timer.cancel(); p.kill()
def ours(h):
    return (str(h.get("eventName", "")).lower() == "pretooluse"
        and h.get("command") == cmd and h.get("enabled")
        and not h.get("async"))
found = [h for h in hooks() if ours(h)]
if not found:
    sys.exit("hook not listed by codex hooks/list")
if not all(h.get("trustStatus") in ("trusted", "managed") for h in found):
    with open(os.path.join(home, "config.toml"), "a") as f:
        for h in found:
            f.write("\n[hooks.state.%s]\ntrusted_hash = %s\n"
                % (json.dumps(h["key"]), json.dumps(h["currentHash"])))
    found = [h for h in hooks() if ours(h)]
if not found or not all(h.get("trustStatus") in ("trusted", "managed") for h in found):
    sys.exit("hook not trusted")
"""


def codex_native_hook_block(*, required: bool) -> str:
    """Shell that installs and trusts the Preloop hook before Codex starts.

    Must run after ``config.toml`` is written and before ``codex exec``.

    Args:
        required: A native deny or approval-required rule applies to the
            flow, so a missing hook fails the run with ``approval_unavailable``.

    Returns:
        A bash fragment.
    """
    amd64 = HOOK_CLI_SHA256["amd64"]
    arm64 = HOOK_CLI_SHA256["arm64"]
    trust = shlex.quote(_TRUST_SCRIPT)
    return f"""
# ============================================================
# Preloop native command policy (PreToolUse hook)
# ============================================================
_pl_hook_required={1 if required else 0}
_pl_hook_error=""
export PRELOOP_FLOW_TOKEN="${{PRELOOP_API_TOKEN:-}}"
export PRELOOP_FLOW_API_URL="${{PRELOOP_FLOW_API_URL:-${{PRELOOP_MCP_URL%/mcp/v1}}}}"
_pl_cli=""
if [ -x {IMAGE_CLI_PATH} ]; then
    _pl_cli={IMAGE_CLI_PATH}
elif command -v preloop >/dev/null 2>&1; then
    _pl_cli="$(command -v preloop)"
else
    case "$(uname -m)" in
        x86_64|amd64) _pl_arch=amd64; _pl_sum={amd64} ;;
        aarch64|arm64) _pl_arch=arm64; _pl_sum={arm64} ;;
        *) _pl_arch="" ;;
    esac
    if [ -n "$_pl_arch" ]; then
        mkdir -p "$HOME/.preloop-hook"
        if curl -fsSL --retry 2 -o "$HOME/.preloop-hook/preloop" \\
            "https://github.com/preloop/preloop/releases/download/v{HOOK_CLI_VERSION}/preloop-linux-$_pl_arch" \\
            && echo "$_pl_sum  $HOME/.preloop-hook/preloop" | sha256sum -c - >/dev/null 2>&1; then
            chmod 755 "$HOME/.preloop-hook/preloop"
            _pl_cli="$HOME/.preloop-hook/preloop"
        else
            rm -f "$HOME/.preloop-hook/preloop"
            _pl_hook_error="the pinned Preloop CLI {HOOK_CLI_VERSION} could not be downloaded and verified"
        fi
    else
        _pl_hook_error="no Preloop CLI build for $(uname -m)"
    fi
fi
if [ -n "$_pl_cli" ] && [ -z "$PRELOOP_FLOW_TOKEN" ]; then
    _pl_hook_error="the execution has no runtime credential for the hook"
fi
if [ -n "$_pl_cli" ] && [ -z "$_pl_hook_error" ] && ! command -v python3 >/dev/null 2>&1; then
    _pl_hook_error="python3 is not available to register the hook with Codex"
fi
if [ -n "$_pl_cli" ] && [ -z "$_pl_hook_error" ]; then
    _pl_hook_cmd="'$_pl_cli' agents permission-hook --source codex_cli --hook-event PreToolUse --require-flow-credential"
    # 330 s is ample: the hook never waits for a human here. The runtime
    # credential names this execution, so an approval-required command parks
    # the run and the hook answers at once (#2137); the decision is applied
    # on resume, not inside this hook call.
    python3 - "$_pl_hook_cmd" > "$CODEX_HOME/hooks.json" <<'PL_HOOKS'
import json, sys
print(json.dumps({{"hooks": {{"PreToolUse": [{{"matcher": "*", "hooks": [
    {{"type": "command", "command": sys.argv[1], "timeout": 330}}]}}]}}}}))
PL_HOOKS
    # Commands the agent runs must not see the runtime credential (under
    # either name); Codex hands its own environment to hooks and MCP
    # servers, not this table.
    printf '\\n[shell_environment_policy.set]\\nPRELOOP_FLOW_TOKEN = ""\\nPRELOOP_API_TOKEN = ""\\n' >> "$CODEX_HOME/config.toml"
    if _pl_trust_out="$(python3 -c {trust} "$_pl_hook_cmd" 2>&1)"; then
        echo "{HOOK_READY_LINE}"
    else
        _pl_hook_error="Codex did not accept the Preloop hook ($_pl_trust_out)"
    fi
fi
if [ -n "$_pl_hook_error" ]; then
    if [ "$_pl_hook_required" = "1" ]; then
        echo "approval_unavailable: native command rules (deny or require approval) apply to this flow and the Preloop command hook could not be installed: $_pl_hook_error. The run was refused instead of running those commands ungated."
        echo "PRELOOP_SETUP_FAILED approval_unavailable"
        exit 78
    fi
    echo "WARNING: Preloop native command hook not installed: $_pl_hook_error; tool rules are not applied to native commands in this run"
fi
"""


#: The unprivileged user hosted Codex runs as (environments/universal/Dockerfile).
AGENT_USER = "preloop-agent"
#: Root-owned launcher the run writes once the agent user is set up.
AGENT_LAUNCHER_PATH = "/usr/local/libexec/preloop-as-agent"
#: Log line naming the user Codex runs as.
AGENT_USER_LINE = "PRELOOP_AGENT_USER"

#: Files in ``$CODEX_HOME`` that stay root-owned and read-only to the agent:
#: everything Codex reads its configuration, hooks, trust state and
#: credentials from. Codex's own runtime state (sessions, logs, sqlite
#: databases) is handed to the agent user.
LOCKED_CODEX_FILES = (
    "config.toml",
    "hooks.json",
    "auth.json",
    "managed_config.toml",
    "requirements.toml",
    "preloop-models.json",
    "preloop-browser-mcp.toml",
    "AGENTS.md",
    "AGENTS.override.md",
)
_LOCKED_CODEX_FILES = " ".join(LOCKED_CODEX_FILES)

# Written to AGENT_LAUNCHER_PATH. The values in angle brackets are filled in
# by the block below. Variables Codex does not need and that carry
# credentials are dropped; Codex itself keeps its model key and the runtime
# credential (the MCP bearer and the hook's flow credential).
_LAUNCHER = r"""#!/bin/bash
# Runs a command as the unprivileged Codex agent user (written per run).
unset MCP_CONFIG_JSON PRELOOP_GIT_CREDENTIALS
for _pl_v in $(compgen -e); do
    case "$_pl_v" in PRELOOP_GIT_TOKEN_*) unset "$_pl_v" ;; esac
done
unset _pl_v
export HOME=<HOME> USER=<USER> LOGNAME=<USER> CODEX_HOME=<CODEX_HOME>
export RUSTUP_HOME="${RUSTUP_HOME:-/root/.rustup}"
export CARGO_HOME="$HOME/.cargo" GOPATH="$HOME/go" GOBIN="$HOME/go/bin"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/go/bin:$PATH"
# Through env: a binary that setpriv itself executes stays dumpable even when
# it is unreadable; one executed after the drop is not (checked in
# test_codex_agent_user_container.py).
exec setpriv --reuid=<USER> --regid=<USER> --init-groups --no-new-privs \
    --inh-caps=-all --bounding-set=-all -- /usr/bin/env -- "$@"
"""


def codex_agent_user_block() -> str:
    """Shell that drops Codex to the unprivileged agent user.

    Runs as root after ``config.toml``, ``hooks.json`` and the hook trust
    state are written (:func:`codex_native_hook_block`) and before
    ``codex exec``. It leaves those files and the hook CLI root-owned and
    read-only to the agent user, makes ``$CODEX_HOME`` a sticky directory
    so the agent can add sessions and logs but cannot replace or remove the
    files root wrote, hands the workspace and the agent's home to the agent
    user, and writes :data:`AGENT_LAUNCHER_PATH`. Every ``codex`` launch
    is prefixed with ``$PRELOOP_AS_AGENT``.

    The ``node`` binary and the native ``codex`` binary are made
    execute-only (0711). The kernel marks a process that runs an
    unreadable binary as not dumpable, so ``/proc/<pid>/environ`` of Codex
    and of the hook CLI is not readable by commands the agent runs as the
    same user. That keeps the runtime credential, which Codex holds for its
    MCP connection and passes to the hook, out of reach of shell commands;
    Codex already removes it from the environment it gives them.

    An image without the agent user, a container that is not root, or a
    missing ``setpriv`` keeps the earlier behaviour (Codex runs as the
    container user) and logs why.

    Returns:
        A bash fragment that sets ``PRELOOP_AS_AGENT``.
    """
    launcher = shlex.quote(_LAUNCHER)
    return f"""
# ============================================================
# Run Codex as the unprivileged agent user
# ============================================================
PRELOOP_AS_AGENT=""
_pl_agent_error=""
rm -f {AGENT_LAUNCHER_PATH} 2>/dev/null || true
if [ "$(id -u)" != "0" ]; then
    _pl_agent_error="the container does not run as root"
elif ! id -u {AGENT_USER} >/dev/null 2>&1; then
    _pl_agent_error="the image has no {AGENT_USER} user"
elif ! command -v setpriv >/dev/null 2>&1; then
    _pl_agent_error="setpriv is not available"
fi
_pl_codex_parent="$(dirname "$CODEX_HOME")"
if [ -z "$_pl_agent_error" ] && {{ [ "$(stat -c %u "$_pl_codex_parent")" != "0" ] \\
    || [ -n "$(find "$_pl_codex_parent" -maxdepth 0 -perm /022)" ]; }}; then
    _pl_agent_error="$_pl_codex_parent is writable by other users"
fi
if [ -z "$_pl_agent_error" ]; then
    _pl_agent_home="$(getent passwd {AGENT_USER} | cut -d: -f6)"
    _pl_agent_home="${{_pl_agent_home:-/home/{AGENT_USER}}}"
    mkdir -p "$_pl_agent_home"
    if [ -f /root/.gitconfig ]; then cp /root/.gitconfig "$_pl_agent_home/.gitconfig"; fi
    chown -R {AGENT_USER}:{AGENT_USER} "$_pl_agent_home"
    # The base image keeps its toolchains under /root: traverse, not list.
    chmod 0711 /root
    # The workspace (and a working directory outside it) belongs to the agent.
    if [ -d /workspace ]; then chown -R {AGENT_USER}:{AGENT_USER} /workspace; fi
    case "$PWD" in
        /|/workspace|/workspace/*|/root|/root/*|/opt|/opt/*|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/lib*|/var|/var/*|/proc*|/sys*|/dev*|/boot*) ;;
        *) chown -R {AGENT_USER}:{AGENT_USER} "$PWD" ;;
    esac
    # Same git access as before: the agent's own fetches use the clone store.
    if [ -f /tmp/.preloop-git-credentials ]; then
        chown {AGENT_USER} /tmp/.preloop-git-credentials
    fi
    # $CODEX_HOME: root-owned and sticky. The agent may add files and owns
    # the subdirectories (sessions, logs); config.toml, hooks.json and the
    # rest of what root wrote stay root-owned, read-only to the agent, and
    # cannot be removed or renamed by it.
    # Empty managed layers stop the agent from adding its own.
    mkdir -p "$CODEX_HOME/sessions"
    for _pl_f in managed_config.toml requirements.toml; do
        [ -e "$CODEX_HOME/$_pl_f" ] || : > "$CODEX_HOME/$_pl_f"
    done
    chown -R {AGENT_USER}:{AGENT_USER} "$CODEX_HOME"
    for _pl_f in {_LOCKED_CODEX_FILES}; do
        if [ -e "$CODEX_HOME/$_pl_f" ] || [ -L "$CODEX_HOME/$_pl_f" ]; then
            chown -h root:{AGENT_USER} "$CODEX_HOME/$_pl_f"
            [ -L "$CODEX_HOME/$_pl_f" ] || chmod 0640 "$CODEX_HOME/$_pl_f"
        fi
    done
    chown root:{AGENT_USER} "$CODEX_HOME"
    chmod 1770 "$CODEX_HOME"
    # The hook CLI and the binaries that hold the runtime credential are
    # root-owned and execute-only: not writable, and not dumpable when run.
    for _pl_bin in "${{_pl_cli:-}}" "$(command -v node 2>/dev/null || true)"; do
        [ -n "$_pl_bin" ] || continue
        _pl_bin="$(readlink -f "$_pl_bin")"
        if [ -f "$_pl_bin" ]; then chown root:root "$_pl_bin"; chmod 0711 "$_pl_bin"; fi
    done
    _pl_npm_root="$(npm root -g 2>/dev/null || true)"
    if [ -n "$_pl_npm_root" ] && [ -d "$_pl_npm_root/@openai" ]; then
        while IFS= read -r -d '' _pl_bin; do
            if [ "$(head -c 4 "$_pl_bin" | od -An -c | tr -d ' ')" = "177ELF" ]; then
                chmod 0711 "$_pl_bin"
            fi
        done < <(find -L "$_pl_npm_root/@openai" -type f -name codex -print0 2>/dev/null)
    fi
    mkdir -p "$(dirname {AGENT_LAUNCHER_PATH})"
    printf '%s' {launcher} \\
        | sed -e "s|<HOME>|$_pl_agent_home|g" -e "s|<USER>|{AGENT_USER}|g" \\
            -e "s|<CODEX_HOME>|$CODEX_HOME|g" > {AGENT_LAUNCHER_PATH}
    chown root:root {AGENT_LAUNCHER_PATH}
    chmod 0755 {AGENT_LAUNCHER_PATH}
    if {AGENT_LAUNCHER_PATH} true; then
        PRELOOP_AS_AGENT={AGENT_LAUNCHER_PATH}
    else
        rm -f {AGENT_LAUNCHER_PATH}
        _pl_agent_error="dropping to {AGENT_USER} failed"
    fi
fi
if [ -n "$PRELOOP_AS_AGENT" ]; then
    echo "{AGENT_USER_LINE} {AGENT_USER}"
else
    echo "{AGENT_USER_LINE} $(id -un 2>/dev/null || id -u) ($_pl_agent_error; Codex can change its own hook configuration in this run)"
fi
"""
