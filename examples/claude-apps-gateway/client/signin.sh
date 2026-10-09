#!/usr/bin/env bash
# Step 1: real Claude Code gateway sign-in in a throwaway HOME.
# Writes the gateway login keys to THIS CONTAINER's managed-settings.json
# (the container is disposable; the host is never touched), runs the
# interactive /login in a PTY, and completes the browser leg with curl.
# Usage: signin.sh <home_dir> <email> <password>
set -euo pipefail
HOME_DIR="$1"; EMAIL="$2"; PASSWORD="$3"
mkdir -p /etc/claude-code "$HOME_DIR"
cat > /etc/claude-code/managed-settings.json <<JSON
{
  "forceLoginMethod": "gateway",
  "forceLoginGatewayUrl": "http://localhost:8080",
  "parentSettingsBehavior": "merge"
}
JSON
WORK="$(mktemp -d)"
export HOME="$HOME_DIR"
cd "$HOME_DIR"
TUI_TIMEOUT=120 python3 /harness-client/tui-login.py "$WORK/code" "$WORK/done" \
  >"$WORK/tui.out" 2>"$WORK/tui.err" &
TUI=$!
for _ in $(seq 1 60); do [ -s "$WORK/code" ] && break; sleep 1; done
[ -s "$WORK/code" ] || { echo "no device code from /login" >&2; tail -c 2000 "$WORK/tui.err" >&2; exit 1; }
bash /harness-client/device-login.sh "$EMAIL" "$PASSWORD" "$(cat "$WORK/code")"
touch "$WORK/done"
if wait "$TUI"; then
  echo "signed in: $EMAIL"
else
  echo "TUI did not report success; checking auth status" >&2
fi
claude auth status </dev/null
