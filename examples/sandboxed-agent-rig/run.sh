#!/usr/bin/env bash
# Sandboxed agent rig driver.
#
# Starts a local Preloop stack with a mocked model provider, then walks an
# ordinary writable home and five sandbox variants through onboarding, the manual change "on the host",
# resume, one real request, a discovery report and offboarding. Transcripts
# of what a user sees are written to ./out with every key redacted.
#
#   ./run.sh              all variants
#   ./run.sh ro-settings  one variant (writable, ro-settings, ro-everything,
#                         wrong-owner, no-login, stale-login)
#   KEEP=1 ./run.sh       leave the stack running afterwards
#
# It never contacts a real provider or a hosted Preloop.
set -euo pipefail
cd "$(dirname "$0")"

export PRELOOP_DISABLE_TELEMETRY=true
VARIANTS=("$@")
if [ ${#VARIANTS[@]} -eq 0 ]; then
  VARIANTS=(writable ro-settings ro-everything wrong-owner no-login stale-login)
fi
OUT=out
FAILURES=0

compose() { docker compose "$@"; }

# Keys never reach a transcript, even throwaway ones from the local stack.
redact() {
  sed -E \
    -e 's/agt_[A-Za-z0-9_-]{8,}/agt_<redacted>/g' \
    -e 's/sk-ant-[A-Za-z0-9_-]{8,}/sk-ant-<redacted>/g' \
    -e 's/plr1\.[A-Za-z0-9_-]{16,}/plr1.<resume token>/g' \
    -e 's/\r$//'
}

note() { printf '\n# %s\n' "$*" | tee -a "$TRANSCRIPT"; }

# run <service> <command>: run a command in a sandbox the way a user would
# type it, append command, output and exit status to the transcript.
run() {
  local service=$1
  shift
  printf '\n$ %s\n' "$*" | tee -a "$TRANSCRIPT"
  set +e
  compose exec -T -e PRELOOP_TOKEN="$RIG_PRELOOP_TOKEN" "$service" bash -c "$*" 2>&1 | redact | tee -a "$TRANSCRIPT"
  LAST_STATUS=${PIPESTATUS[0]}
  set -e
  printf '[exit %s]\n' "$LAST_STATUS" | tee -a "$TRANSCRIPT"
}

# run_tty <service> <keys> <command>: the same, in a terminal, typing <keys>.
# A subscription login is only transferred when a person confirms it, so
# those onboardings need a real terminal. The keys typed are shown.
run_tty() {
  local service=$1 keys=$2
  shift 2
  printf '\n$ %s\n' "$*" | tee -a "$TRANSCRIPT"
  printf '(typed: %s)\n' "$(printf '%s' "$keys" | sed -e 's/\\n/<Enter> /g')" | tee -a "$TRANSCRIPT"
  set +e
  compose exec -T -e PRELOOP_TOKEN="$RIG_PRELOOP_TOKEN" -e RIG_KEYS="$keys" -e RIG_COMMAND="$*" "$service" bash -c \
    '(printf "$RIG_KEYS"; sleep 2) | script -qec "$RIG_COMMAND" /dev/null' 2>&1 | redact | tee -a "$TRANSCRIPT"
  LAST_STATUS=${PIPESTATUS[0]}
  set -e
  printf '[exit %s]\n' "$LAST_STATUS" | tee -a "$TRANSCRIPT"
}

# host <command>: a step the user does on the host, outside the sandbox.
host() {
  printf '\nhost$ %s\n' "$1" | tee -a "$TRANSCRIPT"
}

expect() {
  if ! grep -qF -- "$1" "$TRANSCRIPT"; then
    echo "RIG FAILURE ($CURRENT): transcript does not contain: $1" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

refute() {
  if grep -qF -- "$1" "$TRANSCRIPT"; then
    echo "RIG FAILURE ($CURRENT): transcript must not contain: $1" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

expect_status() {
  if [ "$LAST_STATUS" != "$1" ]; then
    echo "RIG FAILURE ($CURRENT): expected exit $1, got $LAST_STATUS" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

# server_state: what the account holds, counted through the API (no secrets).
server_state() {
  compose exec -T -e RIG_TOKEN="$RIG_PRELOOP_TOKEN" preloop python - <<'PY'
import json, os, urllib.request

def get(path):
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1" + path,
        headers={"Authorization": "Bearer " + os.environ["RIG_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)

agents = get("/agents?limit=100").get("items", [])
models = get("/ai-models")
active = [a for a in agents if a.get("lifecycle_state") == "active"]
print(
    "server state: agents=%d (active=%d, states=%s) ai_models=%d"
    % (
        len(agents),
        len(active),
        sorted({a.get("onboarding_state") or "-" for a in agents}),
        len(models),
    )
)
PY
}

seed_settings() {
  mkdir -p "state/$1/claude"
  cat > "state/$1/claude/settings.json" <<JSON
{
  "model": "claude-sonnet-4-5",
  "permissions": {
    "allow": ["Bash(ls:*)"]
  },
  "env": {
    "ANTHROPIC_BASE_URL": "http://mock-anthropic:8080"$2
  }
}
JSON
}

bootstrap_user() {
  RIG_PRELOOP_TOKEN=$(compose exec -T preloop python - <<'PY'
import json, secrets, urllib.error, urllib.parse, urllib.request

base = "http://127.0.0.1:8000/api/v1"
user = "rig" + secrets.token_hex(4)
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
  )
  export RIG_PRELOOP_TOKEN
}

start_stack() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$OUT" state
  mkdir -p "$OUT"
  seed_settings ro-settings ""
  seed_settings ro-everything ""
  seed_settings no-login ""
  seed_settings stale-login ""
  compose build --quiet
  compose up -d --wait postgres nats mock-anthropic preloop
  compose up -d --wait \
    agent-writable agent-ro-settings agent-ro-everything agent-wrong-owner agent-no-login agent-stale-login
}

begin() {
  CURRENT=$1
  TRANSCRIPT="$OUT/$1.txt"
  : > "$TRANSCRIPT"
  # A fresh account per variant, so one variant's stored credentials cannot
  # back another variant's route.
  bootstrap_user
  printf '# Variant %s: %s\n' "$1" "$2" | tee -a "$TRANSCRIPT"
}

# --- The ordinary case ----------------------------------------------------
variant_writable() {
  local svc=agent-writable
  begin writable "nothing is read-only: an ordinary home, login done inside the container"
  compose exec -T "$svc" bash -c 'umask 077; cat > ~/.claude/settings.json' <<'JSON'
{
  "model": "claude-sonnet-4-5",
  "env": {
    "ANTHROPIC_BASE_URL": "http://mock-anthropic:8080"
  }
}
JSON
  compose exec -T "$svc" bash -c 'umask 077; cat > ~/.claude/.credentials.json' <<JSON
{"claudeAiOauth":{"accessToken":"sk-ant-oat01-rig-synthetic-container-login-0001","refreshToken":"sk-ant-ort01-rig-synthetic-refresh-0001","expiresAt":$(( ($(date +%s) + 86400 * 30) * 1000 )),"scopes":["user:inference"],"subscriptionType":"team"}}
JSON
  # No agent named and one key pressed: Enter at the review.
  run_tty "$svc" '\n' "preloop agents onboard"
  expect_status 0
  expect "Preloop setup on this machine"
  expect "Preloop will hold and rotate the Claude Code subscription login"
  expect "Options: approval hooks off, extension report on, runner off"
  expect "Apply to 1 agent? [Y/n/c/e]:"
  refute "Agent name ["
  refute "Use which model?"
  refute "Onboard Claude Code (Claude Code) into managed Preloop access now?"
  expect "agent and key created in Preloop"
  expect "extension report sent"
  expect "✓ Onboarded Claude Code"
  expect "Gateway validation: passed"
  expect "Again:  preloop agents onboard --agents claude-code"
  server_state | tee -a "$TRANSCRIPT"

  note "One real request from the agent itself, through the gateway."
  run "$svc" "claude -p 'Reply with the single word ACK.' --output-format text"

  note "Cancelling the review changes nothing."
  run_tty "$svc" 'n\n' "preloop agents onboard"
  expect_status 0
  expect "Nothing was changed."

  note "Without a terminal the plan is printed and nothing is applied."
  run "$svc" "preloop agents onboard --no-report-extensions"
  expect_status 1
  expect "onboarding requires a terminal"
  expect "To apply it without a terminal: PRELOOP_CONFIRM=1 preloop agents onboard --agents claude-code"

  run "$svc" "preloop agents offboard 'Claude Code' --yes --remove-model yes --remove-mcp-servers no"
  expect_status 0
  expect "✓ Offboarded Claude Code"
  server_state | tee -a "$TRANSCRIPT"
}

# --- Variant 1 ------------------------------------------------------------
variant_ro_settings() {
  local svc=agent-ro-settings
  begin ro-settings "settings.json bind-mounted read-only from the host, ~/.preloop writable, login done inside the container"
  # "A login done inside the container": a synthetic credentials file the
  # mock accepts. This is setup, not part of what the user types.
  compose exec -T "$svc" bash -c 'umask 077; cat > ~/.claude/.credentials.json' <<JSON
{"claudeAiOauth":{"accessToken":"sk-ant-oat01-rig-synthetic-container-login-0001","refreshToken":"sk-ant-ort01-rig-synthetic-refresh-0001","expiresAt":$(( ($(date +%s) + 86400 * 30) * 1000 )),"scopes":["user:inference"],"subscriptionType":"team"}}
JSON

  run "$svc" 'id -u; stat -c "%u %a %n" ~/.claude/settings.json; grep " /home/agent/.claude/settings.json " /proc/self/mountinfo | cut -d" " -f4-6'
  # Enter at the one review (apply with the defaults), then "later" at the
  # wait prompt: the change is made in a second step.
  run_tty "$svc" '\nlater\n' "preloop agents onboard 'Claude Code'"
  expect_status 3
  expect "Apply to 1 agent? [Y/n/c/e]:"
  refute "Agent name ["
  refute "Use which model?"
  expect "Running inside a container"
  # Linux reports the foreign owner first, Docker Desktop the read-only
  # mount; the mount line is printed in both cases.
  expect "mounted read-only at /home/agent/.claude/settings.json"
  expect "Onboarding continues in manual mode"
  expect "Merge these keys into the file and keep everything else:"
  expect "Waiting for local configuration: Claude Code is NOT onboarded yet."
  refute "✓ Onboarded"
  server_state | tee -a "$TRANSCRIPT"

  note "Resume before the change is in place: it says exactly what is missing."
  run "$svc" "preloop agents onboard --resume 'Claude Code'"
  expect_status 1
  expect "env.ANTHROPIC_API_KEY is missing"

  note "The user edits the file on the host and leaves a trailing comma."
  host "cp <complete result from the sandbox> state/ro-settings/claude/settings.json   # with a trailing comma"
  compose exec -T "$svc" bash -c 'cat "$(ls -d ~/.preloop/manual-setup/* | grep -v -- -offboard | head -1)/settings.json"' > "$OUT/.ro-settings.result.json"
  python3 - "$OUT/.ro-settings.result.json" state/ro-settings/claude/settings.json <<'PY'
import sys
text = open(sys.argv[1]).read().rstrip()
assert text.endswith("}")
# Overwrite in place: a single-file bind mount follows the inode.
with open(sys.argv[2], "r+") as handle:
    handle.seek(0)
    handle.write(text[:-1].rstrip() + ",\n}\n")
    handle.truncate()
PY
  run "$svc" "preloop agents onboard --resume 'Claude Code'"
  expect_status 1
  expect "invalid JSON at line"
  expect "trailing comma"

  note "The user fixes the file on the host: the complete result, copied over it."
  host "cp <complete result from the sandbox> state/ro-settings/claude/settings.json"
  python3 - "$OUT/.ro-settings.result.json" state/ro-settings/claude/settings.json <<'PY'
import sys
with open(sys.argv[2], "r+") as handle:
    handle.seek(0)
    handle.write(open(sys.argv[1]).read())
    handle.truncate()
PY
  rm -f "$OUT/.ro-settings.result.json"
  run "$svc" "preloop agents onboard --resume 'Claude Code'"
  expect_status 0
  expect "✓ Onboarded Claude Code"
  expect "Gateway validation: passed"
  server_state | tee -a "$TRANSCRIPT"

  note "One real request from the agent itself, through the gateway."
  run "$svc" "claude -p 'Reply with the single word ACK.' --output-format text"
  ONBOARD_END=$(wc -l < "$TRANSCRIPT")

  run "$svc" "preloop agents discover --report --no-onboard-prompt"
  run "$svc" "preloop agents status 'Claude Code'"

  note "Offboard: the file is still read-only, so the removal is printed first."
  run "$svc" "preloop agents offboard 'Claude Code' --yes --remove-model yes --remove-mcp-servers no"
  expect_status 3
  expect "Manual removal for Claude Code"
  expect "Nothing has been changed in Preloop yet"
  refute "✓ Offboarded"
  host "cp <complete result from the sandbox> state/ro-settings/claude/settings.json"
  compose exec -T "$svc" bash -c 'cat ~/.preloop/manual-setup/*-offboard/settings.json' > "$OUT/.ro-settings.offboard.json"
  python3 - "$OUT/.ro-settings.offboard.json" state/ro-settings/claude/settings.json <<'PY'
import sys
with open(sys.argv[2], "r+") as handle:
    handle.seek(0)
    handle.write(open(sys.argv[1]).read())
    handle.truncate()
PY
  rm -f "$OUT/.ro-settings.offboard.json"
  run "$svc" "preloop agents offboard 'Claude Code' --yes --remove-model yes --remove-mcp-servers no"
  expect_status 0
  expect "✓ Offboarded Claude Code"
  expect "holds no Preloop configuration; nothing to change."
  server_state | tee -a "$TRANSCRIPT"
}

# --- Variant 2 ------------------------------------------------------------
variant_ro_everything() {
  local svc=agent-ro-everything
  begin ro-everything "read-only root filesystem and settings, ~/.preloop not writable, login from CLAUDE_CODE_OAUTH_TOKEN"
  run "$svc" "touch ~/.preloop/probe 2>&1 | head -1; touch ~/.claude/probe 2>&1 | head -1"
  run_tty "$svc" '\nlater\n' "preloop agents onboard 'Claude Code' --manual-output /tmp/preloop-setup"
  expect_status 3
  expect "Preloop state (backup, resume and hook credentials)"
  expect "subscription login from CLAUDE_CODE_OAUTH_TOKEN"
  expect "Resume token (not a secret; keep it, ~/.preloop is not writable)"
  expect "--resume-token"

  note "The user chooses the environment-variable form (a Compose env_file) instead of editing the file."
  run "$svc" 'while IFS="=" read -r key value; do export "$key=$value"; done < /tmp/preloop-setup/preloop.env; preloop agents onboard --resume --from-env --resume-token "$(cat /tmp/preloop-setup/resume-token.txt)"'
  expect_status 0
  expect "✓ Onboarded Claude Code"
  expect "Configuration applied by you: environment variables"
  server_state | tee -a "$TRANSCRIPT"

  note "Offboard: nothing local to change (the variables live in the user's Compose file)."
  run "$svc" "preloop agents offboard 'Claude Code' --yes --remove-model yes --remove-mcp-servers no"
  expect_status 0
  expect "✓ Offboarded Claude Code"
  expect "never rotated by Preloop; nothing to write back"
  server_state | tee -a "$TRANSCRIPT"
}

# --- Variant 3 ------------------------------------------------------------
variant_wrong_owner() {
  local svc=agent-wrong-owner
  begin wrong-owner "the mount is writable but settings.json belongs to another uid; upstream is an API key"
  compose exec -T -u 0 "$svc" bash -c 'cat > /home/agent/.claude/settings.json; chown -R 0:0 /home/agent/.claude; chmod 755 /home/agent/.claude; chmod 644 /home/agent/.claude/settings.json' <<'JSON'
{
  "model": "claude-sonnet-4-5",
  "env": {
    "ANTHROPIC_BASE_URL": "http://mock-anthropic:8080",
    "ANTHROPIC_API_KEY": "sk-ant-api03-rig-synthetic-upstream-key-0001"
  }
}
JSON
  run "$svc" 'id -u; stat -c "%u %a %n" ~/.claude ~/.claude/settings.json'
  run_tty "$svc" '' "preloop agents onboard 'Claude Code' --yes"
  expect_status 3
  expect "permission denied (owned by uid 0, running as uid 10001)"
  refute "✓ Onboarded"

  note "The file's owner applies the complete result."
  host "sudo cp <complete result> <the mounted settings.json>"
  compose exec -T -u 0 "$svc" bash -c 'cat "$(ls -d /home/agent/.preloop/manual-setup/* | grep -v -- -offboard | head -1)/settings.json" > /home/agent/.claude/settings.json'
  run "$svc" "preloop agents onboard --resume 'Claude Code'"
  expect_status 0
  expect "✓ Onboarded Claude Code"
  expect "Gateway validation: passed"

  note "Offboard, removing the block as the file's owner."
  run "$svc" "preloop agents offboard 'Claude Code' --yes --remove-model yes --remove-mcp-servers no"
  expect_status 3
  host "sudo cp <complete result> <the mounted settings.json>"
  compose exec -T -u 0 "$svc" bash -c 'cat /home/agent/.preloop/manual-setup/*-offboard/settings.json > /home/agent/.claude/settings.json'
  run "$svc" "preloop agents offboard 'Claude Code' --yes --remove-model yes --remove-mcp-servers no"
  expect_status 0
  expect "✓ Offboarded Claude Code"
  server_state | tee -a "$TRANSCRIPT"
}

# --- Variant 4 ------------------------------------------------------------
variant_no_login() {
  local svc=agent-no-login
  begin no-login "read-only settings and no Claude login anywhere"
  run_tty "$svc" '' "preloop agents onboard 'Claude Code' --yes"
  expect_status 3
  host "cp <complete result from the sandbox> state/no-login/claude/settings.json"
  compose exec -T "$svc" bash -c 'cat "$(ls -d ~/.preloop/manual-setup/* | grep -v -- -offboard | head -1)/settings.json"' > "$OUT/.no-login.json"
  python3 - "$OUT/.no-login.json" state/no-login/claude/settings.json <<'PY'
import sys
with open(sys.argv[2], "r+") as handle:
    handle.seek(0)
    handle.write(open(sys.argv[1]).read())
    handle.truncate()
PY
  rm -f "$OUT/.no-login.json"
  run "$svc" "preloop agents onboard --resume 'Claude Code'"
  expect_status 0
  expect "✓ Onboarded Claude Code"
  refute "Gateway validation: passed"
  run "$svc" "preloop agents onboard --resume 'Claude Code'"
  server_state | tee -a "$TRANSCRIPT"
}

# --- Variant 5 ------------------------------------------------------------
variant_stale_login() {
  local svc=agent-stale-login
  begin stale-login "read-only settings and a login the provider rejects"
  run_tty "$svc" '\nlater\n' "preloop agents onboard 'Claude Code'"
  expect_status 3
  host "cp <complete result from the sandbox> state/stale-login/claude/settings.json"
  compose exec -T "$svc" bash -c 'cat "$(ls -d ~/.preloop/manual-setup/* | grep -v -- -offboard | head -1)/settings.json"' > "$OUT/.stale-login.json"
  python3 - "$OUT/.stale-login.json" state/stale-login/claude/settings.json <<'PY'
import sys
with open(sys.argv[2], "r+") as handle:
    handle.seek(0)
    handle.write(open(sys.argv[1]).read())
    handle.truncate()
PY
  rm -f "$OUT/.stale-login.json"
  run "$svc" "preloop agents onboard --resume 'Claude Code'"
  expect_status 1
  refute "✓ Onboarded"
  expect "Anthropic rejected the Claude subscription login Preloop imported."
  expect "It was read from the environment variable CLAUDE_CODE_OAUTH_TOKEN."
  expect "looks stale"
  server_state | tee -a "$TRANSCRIPT"

  note "Giving up: cancel removes what onboarding created."
  run "$svc" "preloop agents onboard --cancel 'Claude Code'"
  expect_status 0
  expect "Cancelled the pending setup"
  server_state | tee -a "$TRANSCRIPT"
}

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

start_stack
for variant in "${VARIANTS[@]}"; do
  "variant_${variant//-/_}"
done

if grep -rEl 'agt_[A-Za-z0-9_-]{20,}|sk-ant-[a-z0-9]+-rig' "$OUT" >/dev/null 2>&1; then
  echo "RIG FAILURE: a transcript contains an unredacted key" >&2
  FAILURES=$((FAILURES + 1))
fi
echo
if [ "$FAILURES" -ne 0 ]; then
  echo "Sandboxed agent rig: $FAILURES check(s) failed. Transcripts: $OUT/"
  exit 1
fi
echo "Sandboxed agent rig: all checks passed. Transcripts: $OUT/"
