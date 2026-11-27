#!/usr/bin/env bash
# Measure what a person goes through to onboard an agent.
#
# Starts the sandbox rig's local stack (a local Preloop, a mocked model
# provider, agent containers), runs `preloop agents onboard` in a real
# pseudo-terminal 80 columns wide, answers each prompt with its default,
# and prints for each variant:
#
#   interactions      answers the person had to give
#   rows before       rows printed before the apply decision, at 80 columns
#   rows total        rows printed by the whole command
#   longest line      in characters
#   seconds to probe  from the first prompt to the gateway probe result
#
#   cli/scripts/measure-onboarding.sh                  writable and ro-settings
#   cli/scripts/measure-onboarding.sh writable         one variant
#   KEEP=1 cli/scripts/measure-onboarding.sh           leave the stack running
#   OUT=dir cli/scripts/measure-onboarding.sh          where transcripts go
#   RIG_DIR=path/to/sandboxed-agent-rig ...            measure another checkout
#   MAX_INTERACTIONS=1 MAX_ROWS_BEFORE=18 ...          fail above a threshold
#
# It never contacts a real provider or a hosted Preloop, and sends no
# telemetry.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
RIG_DIR=${RIG_DIR:-$SCRIPT_DIR/../../examples/sandboxed-agent-rig}
RIG_DIR=$(cd "$RIG_DIR" && pwd)
DRIVER=$SCRIPT_DIR/onboarding_pty_driver.py
OUT=${OUT:-$SCRIPT_DIR/../build/onboarding-measure}
COLUMNS_WIDE=${MEASURE_COLUMNS:-80}
ONBOARD_COMMAND=${ONBOARD_COMMAND:-preloop agents onboard}
export PRELOOP_DISABLE_TELEMETRY=true

VARIANTS=("$@")
if [ ${#VARIANTS[@]} -eq 0 ]; then
  VARIANTS=(writable ro-settings)
fi
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

compose() { (cd "$RIG_DIR" && docker compose "$@"); }

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

bootstrap_user() {
  compose exec -T preloop python - <<'PY'
import json, secrets, urllib.parse, urllib.request

base = "http://127.0.0.1:8000/api/v1"
user = "measure" + secrets.token_hex(4)
password = secrets.token_urlsafe(24)
body = json.dumps(
    {"username": user, "email": user + "@example.com", "password": password}
).encode()
request = urllib.request.Request(
    base + "/auth/register", data=body, headers={"Content-Type": "application/json"}
)
urllib.request.urlopen(request, timeout=30).read()
form = urllib.parse.urlencode({"username": user, "password": password}).encode()
with urllib.request.urlopen(base + "/auth/token", data=form, timeout=30) as response:
    print(json.load(response)["access_token"])
PY
}

# A synthetic subscription login the mocked provider accepts.
seed_login() {
  compose exec -T "$1" bash -c 'umask 077; cat > ~/.claude/.credentials.json' <<JSON
{"claudeAiOauth":{"accessToken":"sk-ant-oat01-rig-synthetic-container-login-0001","refreshToken":"sk-ant-ort01-rig-synthetic-refresh-0001","expiresAt":$(( ($(date +%s) + 86400 * 30) * 1000 )),"scopes":["user:inference"],"subscriptionType":"team"}}
JSON
}

settings_json() {
  cat <<'JSON'
{
  "model": "claude-sonnet-4-5",
  "permissions": {
    "allow": ["Bash(ls:*)"]
  },
  "env": {
    "ANTHROPIC_BASE_URL": "http://mock-anthropic:8080"
  }
}
JSON
}

# measure <variant> <service> <label> [driver arguments...]
measure() {
  local variant=$1 service=$2 label=$3
  shift 3
  local token
  token=$(bootstrap_user)
  # shellcheck disable=SC2086
  compose exec -T -e PRELOOP_TOKEN="$token" -e TERM="${MEASURE_TERM:-xterm-256color}" "$service" \
    python3 - --columns "$COLUMNS_WIDE" \
    --answer "change is in place=later" "$@" -- $ONBOARD_COMMAND \
    < "$DRIVER" > "$OUT/$variant.$label.json"
  # Leave the account as it was found: the next run starts clean.
  compose exec -T -e PRELOOP_TOKEN="$token" "$service" bash -c \
    "preloop agents onboard --cancel 'Claude Code' >/dev/null 2>&1; preloop agents offboard 'Claude Code' --yes --remove-model yes --remove-mcp-servers no >/dev/null 2>&1; true"
}

start_stack() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$RIG_DIR/state"
  for variant in ro-settings ro-everything no-login stale-login; do
    mkdir -p "$RIG_DIR/state/$variant/claude"
    settings_json > "$RIG_DIR/state/$variant/claude/settings.json"
  done
  compose build --quiet
  compose up -d --wait postgres nats mock-anthropic preloop
  compose up -d --wait agent-writable agent-ro-settings
}

start_stack
for variant in "${VARIANTS[@]}"; do
  case "$variant" in
    writable)
      service=agent-writable
      settings_json | compose exec -T "$service" bash -c 'cat > ~/.claude/settings.json'
      ;;
    ro-settings)
      service=agent-ro-settings
      ;;
    *)
      echo "unknown variant: $variant (writable, ro-settings)" >&2
      exit 2
      ;;
  esac
  seed_login "$service"
  measure "$variant" "$service" tty
done

python3 - "$OUT" "${MAX_INTERACTIONS:-}" "${MAX_ROWS_BEFORE:-}" "${VARIANTS[@]}" <<'PY'
import json, sys

out, max_interactions, max_rows = sys.argv[1], sys.argv[2], sys.argv[3]
failures = []
print()
print("%-12s %12s %12s %10s %12s %16s %5s" % (
    "variant", "interactions", "rows before", "rows total", "longest line",
    "seconds to probe", "exit"))
for variant in sys.argv[4:]:
    with open(f"{out}/{variant}.tty.json") as handle:
        data = json.load(handle)
    with open(f"{out}/{variant}.tty.txt", "w") as handle:
        handle.write(data["transcript"] + "\n")
    seconds = data["seconds_first_prompt_to_probe"]
    before = data["rows_before_decision"]
    print("%-12s %12d %12s %10d %12d %16s %5d" % (
        variant, data["interactions"], "-" if before is None else before,
        data["rows_total"], data["longest_line"],
        "-" if seconds is None else seconds, data["exit_status"]))
    if max_interactions and data["interactions"] > int(max_interactions):
        failures.append(f"{variant}: {data['interactions']} interactions, limit {max_interactions}")
    if max_rows and before is not None and before > int(max_rows):
        failures.append(f"{variant}: {before} rows before the decision, limit {max_rows}")
print()
print(f"Transcripts (keys redacted) and raw measurements: {out}/")
for failure in failures:
    print("MEASURE FAILURE: " + failure, file=sys.stderr)
sys.exit(1 if failures else 0)
PY
