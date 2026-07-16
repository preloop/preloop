#!/usr/bin/env bash
# Module 06 — wait for the instance to answer healthily on the public HTTPS
# URL with TLS verification ON (no -k, ever).

set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

TIMEOUT="${RIG_HEALTH_TIMEOUT:-300}"
deadline=$((SECONDS + TIMEOUT))

rig_log "polling $RIG_URL/api/v1/health (timeout ${TIMEOUT}s, TLS verify on)"
while :; do
  if body=$(curl -fsS --max-time 10 "$RIG_URL/api/v1/health" 2>/dev/null); then
    rig_log "health: $body"
    echo "$body" > "$RIG_RUN_DIR/state/health.json"
    break
  fi
  [ $SECONDS -lt $deadline ] || rig_die "instance not healthy after ${TIMEOUT}s"
  sleep 5
done

# Console must serve the SPA over the same origin.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$RIG_URL/")
[ "$code" = "200" ] || rig_die "console returned HTTP $code at $RIG_URL/"

# Gateway surface must be reachable through the same origin (401 without a
# token is the healthy signal; 404 would mean the proxy route is missing).
gcode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$RIG_URL/openai/v1/models")
case "$gcode" in
  401|403) rig_log "gateway surface answers ($gcode without token) — OK" ;;
  *) rig_note "WARNING: $RIG_URL/openai/v1/models returned $gcode (expected 401)" ;;
esac

rig_note "healthy at $RIG_URL (console 200, api health ok, gateway $gcode)"
