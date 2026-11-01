#!/bin/bash
# Checks the unprivileged agent user hosted Codex runs as.
#
# Runs as root through /opt/entrypoint.sh: at build time from the image copy,
# and from run-smoke.sh over stdin so the checkout's guard checks the final
# image. The Preloop CLI and its directories must be root-owned and not
# writable by the agent user, the agent user must be able to run the CLI,
# and the language smoke (with the Maven and Perl fixtures) must pass as
# that user. Every agent command reads /dev/null: run-smoke.sh feeds this
# script over stdin and a child must not consume the rest of it.
# backend/preloop/agents/codex_native_hook.py drops to the same
# user with the same environment.
set -euo pipefail

user=preloop-agent
cli=/opt/preloop/bin/preloop
smoke=/opt/preloop-universal/smoke.sh

fail() {
    echo "agent-user-check: FAIL: $*" >&2
    exit 1
}

[ "$(id -u)" = "0" ] || fail "run this check as root"
id -u "$user" >/dev/null 2>&1 || fail "missing user: $user"
[ "$(id -u "$user")" != "0" ] || fail "$user must not be uid 0"
command -v setpriv >/dev/null 2>&1 || fail "missing required tool: setpriv"
home="$(getent passwd "$user" | cut -d: -f6)"

as_agent() {
    env HOME="$home" USER="$user" LOGNAME="$user" \
        RUSTUP_HOME="${RUSTUP_HOME:-/root/.rustup}" \
        CARGO_HOME="$home/.cargo" GOPATH="$home/go" GOBIN="$home/go/bin" \
        PATH="$home/.local/bin:$home/.cargo/bin:$home/go/bin:$PATH" \
        setpriv --reuid="$user" --regid="$user" --init-groups --no-new-privs \
        --inh-caps=-all --bounding-set=-all -- "$@" </dev/null
}

for path in /opt/preloop /opt/preloop/bin "$cli"; do
    [ -e "$path" ] || fail "missing: $path"
    [ "$(stat -c %u "$path")" = "0" ] || fail "$path is not owned by root"
    if as_agent test -w "$path"; then
        fail "$path is writable by $user"
    fi
done
if as_agent sh -c ": > '$cli'" 2>/dev/null; then
    fail "$user could overwrite $cli"
fi
if as_agent test -r "$cli"; then
    fail "$cli is readable by $user; it must be execute-only so the hook is not dumpable"
fi
PRELOOP_DISABLE_TELEMETRY=true as_agent "$cli" agents permission-hook --help >/dev/null \
    || fail "$user cannot run $cli"
as_agent test -w "$home" || fail "$home is not writable by $user"

set +e
out="$(cd /tmp && as_agent bash "$smoke" 2>&1)"
status=$?
set -e
if [ "$status" -ne 0 ]; then
    printf '%s\n' "$out" >&2
    fail "the language smoke failed as $user"
fi
for lang in python java go rust typescript perl shell; do
    grep -qx "PASS ${lang}" <<<"$out" || fail "missing PASS ${lang} as $user"
done
echo "agent-user-check: OK ($user)"
