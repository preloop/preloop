#!/usr/bin/env sh
# Release smoke test for the Preloop OSS stack.
#
# Boots docker-compose.release.yaml exactly the way the OSS installer does,
# then verifies the deployment is actually usable:
#   1. API, gateway, and console respond over HTTP
#   2. First-user sign-up and login succeed end-to-end (API -> DB)
#   3. Every service stays up (no restart loops) for a stability window
#
# This is the automated version of the "Release smoke test" checklist in
# README.md. It is run by the release workflow before the GitHub release is
# published, and can be run locally:
#
#   PRELOOP_VERSION=0.10.0 ./scripts/release_smoke_test.sh
#
# Environment variables:
#   PRELOOP_VERSION    - Image tag to test (default: contents of ./VERSION)
#   COMPOSE_FILE       - Compose file to boot (default: ./docker-compose.release.yaml)
#   PROJECT_NAME       - Docker compose project name (default: preloop-smoke)
#   STARTUP_TIMEOUT    - Seconds to wait for the API to become healthy (default: 300)
#   STABILITY_WINDOW   - Seconds services must stay up without restarts (default: 60)
#   KEEP_STACK         - Set to 1 to skip teardown for debugging (default: 0)

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PRELOOP_VERSION="${PRELOOP_VERSION:-$(cat "$REPO_ROOT/VERSION")}"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/docker-compose.release.yaml}"
PROJECT_NAME="${PROJECT_NAME:-preloop-smoke}"
STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-300}"
STABILITY_WINDOW="${STABILITY_WINDOW:-60}"
KEEP_STACK="${KEEP_STACK:-0}"

API_URL="http://localhost:8000"
GATEWAY_URL="http://localhost:8001"
CONSOLE_URL="http://localhost:3000"

WORK_DIR="$(mktemp -d)"
FAILURES=0

log() { printf '\033[1;34m[smoke]\033[0m %s\n' "$*"; }
pass() { printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
fail() {
  printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2
  FAILURES=$((FAILURES + 1))
}

compose() {
  docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" --project-directory "$WORK_DIR" "$@"
}

dump_diagnostics() {
  log "Container status:"
  compose ps -a || true
  log "Recent logs:"
  compose logs --tail 80 || true
}

cleanup() {
  status=$?
  if [ "$KEEP_STACK" = "1" ]; then
    log "KEEP_STACK=1 - leaving stack running (project: $PROJECT_NAME)"
  else
    compose down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK_DIR"
  exit "$status"
}
trap cleanup EXIT INT TERM

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    dd if=/dev/urandom bs=32 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n'
  fi
}

wait_for_http() {
  url="$1"
  timeout="$2"
  label="$3"
  elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  fail "$label did not respond at $url within ${timeout}s"
  return 1
}

# ---------------------------------------------------------------------------
# Boot the stack the same way install-oss.sh does
# ---------------------------------------------------------------------------

log "Testing Preloop OSS ${PRELOOP_VERSION} using ${COMPOSE_FILE}"

cat > "$WORK_DIR/.env" <<EOF
PRELOOP_VERSION=${PRELOOP_VERSION}
SECRET_KEY=$(generate_secret)
POSTGRES_PASSWORD=$(generate_secret)
EOF

log "Pulling images..."
compose pull --quiet

log "Starting stack..."
if ! compose up -d --wait --wait-timeout "$STARTUP_TIMEOUT"; then
  fail "docker compose up did not reach a healthy steady state"
  dump_diagnostics
  exit 1
fi

# ---------------------------------------------------------------------------
# HTTP surface checks
# ---------------------------------------------------------------------------

if wait_for_http "$API_URL/api/v1/health" "$STARTUP_TIMEOUT" "API health"; then
  pass "API health responds"
fi
if wait_for_http "$GATEWAY_URL/api/v1/health" 60 "Gateway health"; then
  pass "Gateway health responds"
fi
if wait_for_http "$CONSOLE_URL/" 60 "Console"; then
  pass "Console responds"
fi

# The browser reaches the API *through* the console, not on port 8000. A console
# that serves its static bundle but cannot proxy /api is a dead install — you
# cannot even log in — so check the path the browser actually uses.
if wait_for_http "$CONSOLE_URL/api/v1/health" 60 "Console -> API proxy"; then
  pass "Console proxies /api to the API"
else
  fail "Console cannot reach the API (check API_URL and the nginx resolver)"
fi

# ---------------------------------------------------------------------------
# First-user sign-up and login (end-to-end API + DB path)
# ---------------------------------------------------------------------------

SMOKE_USER="smoke$(date +%s)"
# Use example.com — EmailStr (email-validator) rejects reserved TLDs like .test
SMOKE_EMAIL="${SMOKE_USER}@example.com"
SMOKE_PASSWORD="$(generate_secret)"

register_status="$(curl -sS -o "$WORK_DIR/register.json" -w '%{http_code}' \
  -X POST "$API_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${SMOKE_USER}\",\"email\":\"${SMOKE_EMAIL}\",\"password\":\"${SMOKE_PASSWORD}\"}" \
  || echo 000)"
if [ "$register_status" = "201" ]; then
  pass "User registration succeeds"
else
  fail "User registration returned HTTP ${register_status}: $(cat "$WORK_DIR/register.json" 2>/dev/null | head -c 300)"
fi

token_status="$(curl -sS -o "$WORK_DIR/token.json" -w '%{http_code}' \
  -X POST "$API_URL/api/v1/auth/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=${SMOKE_USER}" \
  --data-urlencode "password=${SMOKE_PASSWORD}" \
  || echo 000)"
if [ "$token_status" = "200" ] && grep -q '"access_token"' "$WORK_DIR/token.json"; then
  pass "Login returns an access token"
else
  fail "Login returned HTTP ${token_status}: $(cat "$WORK_DIR/token.json" 2>/dev/null | head -c 300)"
fi

# ---------------------------------------------------------------------------
# Stability window: catch restart loops (e.g. issue #53's NATS lame-duck
# healthcheck) that only show up after the stack reports healthy once.
# ---------------------------------------------------------------------------

log "Watching for restart loops for ${STABILITY_WINDOW}s..."
sleep "$STABILITY_WINDOW"

stable=1
for cid in $(compose ps -aq); do
  name="$(docker inspect --format '{{.Name}}' "$cid" | sed 's|^/||')"
  service="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$cid")"
  restarts="$(docker inspect --format '{{.RestartCount}}' "$cid")"
  state="$(docker inspect --format '{{.State.Status}}' "$cid")"
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$cid")"

  if [ "$service" = "migrate" ]; then
    if [ "$state" = "exited" ] && [ "$exit_code" = "0" ]; then
      continue
    fi
    fail "migrate job did not exit cleanly (state=${state}, exit=${exit_code})"
    stable=0
    continue
  fi

  if [ "$restarts" != "0" ] || [ "$state" != "running" ]; then
    fail "${name} is unstable (state=${state}, restarts=${restarts})"
    stable=0
  fi
done
if [ "$stable" = "1" ]; then
  pass "All services stayed up with zero restarts for ${STABILITY_WINDOW}s"
fi

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

if [ "$FAILURES" -gt 0 ]; then
  fail "Smoke test FAILED with ${FAILURES} failure(s)"
  dump_diagnostics
  exit 1
fi

pass "Release smoke test passed for Preloop OSS ${PRELOOP_VERSION}"
