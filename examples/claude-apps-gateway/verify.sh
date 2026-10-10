#!/usr/bin/env bash
# End-to-end verification: Claude Code -> Claude apps gateway -> Preloop.
# Test-only. Runs every Claude Code command inside the `client` container
# with a throwaway HOME; the host's own Claude config, managed preferences,
# registry and /etc/claude-desktop are never read or written.
#
# Usage: ./verify.sh            bring the stack up (if needed) and run 1-8
#        STRICT=1 ./verify.sh   treat PENDING (backend contract absent) as FAIL
#        KEEP_UP=0 ./verify.sh  run `docker compose down -v` at the end
#        IDP_ONLY=1 ./verify.sh only step 7 (Claude Desktop IdP token, #1414);
#                               starts Preloop, dex-idp and the stub model only
set -uo pipefail
export PRELOOP_DISABLE_TELEMETRY=true
cd "$(dirname "$0")" || exit 2
[ -f .env ] || cp .env.example .env
set -a; . ./.env; set +a

PRELOOP="http://127.0.0.1:${HARNESS_PRELOOP_PORT:-18900}"
RECORDER="http://127.0.0.1:19000"
SPARE="http://127.0.0.1:19100"
RUN_DIR="runs/$(date -u +%Y%m%dT%H%M%SZ)"
REPORT="$RUN_DIR/report.md"
mkdir -p "$RUN_DIR"
PASS=0; FAIL=0; PENDING=0
DC() { docker compose "$@"; }
CX() { docker compose exec -T client bash -c "$1"; }

log() { printf '%s\n' "$*" | tee -a "$REPORT"; }
result() { # result <PASS|FAIL|PENDING> <step> <detail>
  case "$1" in PASS) PASS=$((PASS + 1));; FAIL) FAIL=$((FAIL + 1));; PENDING) PENDING=$((PENDING + 1));; esac
  log "- **$1** $2: $3"
}
# check <step> <contract:0|1> <detail> <command...>: contract checks become
# PENDING (not FAIL) when the #1409 backend contract is not deployed.
check() {
  local step="$1" contract="$2" detail="$3"; shift 3
  if "$@"; then result PASS "$step" "$detail"
  elif [ "$contract" = 1 ] && [ "$CONTRACT" = 0 ] && [ "${STRICT:-0}" != 1 ]; then
    result PENDING "$step" "$detail (backend contract #1409 not deployed)"
  else result FAIL "$step" "$detail"; fi
}
api() { local path="$1"; shift; curl -s -H "Authorization: Bearer $ADMIN_KEY" "$@" "$PRELOOP/api/v1$path"; }
latest_usage() { # latest_usage <api_key_id> -> newest usage item JSON
  api "/account/gateway-usage/search?api_key_id=$1&limit=1" | jq -c '.items[0] // {}'
}
wait_usage_after() { # wait until the newest row for key $1 differs from $2
  local row
  for _ in $(seq 1 20); do
    row="$(latest_usage "$1")"
    [ "$(jq -r '.api_usage_id // ""' <<<"$row")" != "$2" ] && { echo "$row"; return; }
    sleep 1
  done
  echo "$row"
}
newest_id() { latest_usage "$1" | jq -r '.api_usage_id // ""'; }

# ------------------------- step 7: Claude Desktop IdP token as bearer (#1414)
# Dex (dex-idp, https) issues an ID token for the Desktop client id; Preloop
# verifies it against the provider registered here and serves the request.
idp_step() {
  api "/account/gateway-identity-providers" | jq -r '.[]? | .id' | while read -r id; do
    api "/account/gateway-identity-providers/$id" -X DELETE >/dev/null
  done
  local created provider_id token status row meta before tampered
  created="$(api "/account/gateway-identity-providers" -X POST -H 'content-type: application/json' -d "$(jq -nc \
    --arg key "$IDP_KEY_ID" '{name:"harness dex",issuer:"https://dex-idp:5557/dex",audiences:["claude-desktop"],
      api_key_id:$key,allowed_email_domains:["example.com"],allow_private_network_issuer:true}')")"
  echo "$created" | jq . >"$RUN_DIR/step7-provider.json"
  provider_id="$(jq -r '.id // ""' <<<"$created")"
  check "7 provider" 0 "identity provider registered through the admin API (id ${provider_id:-none})" test -n "$provider_id"
  api "/account/gateway-identity-providers/$provider_id/test" -X POST >"$RUN_DIR/step7-test.json"
  check "7 discovery" 0 "POST .../test fetched discovery and JWKS ($(jq -r '[.keys[]?.kid] | length' "$RUN_DIR/step7-test.json") keys)" \
    test "$(jq -r .ok "$RUN_DIR/step7-test.json")" = true
  token="$(DC exec -T preloop python /harness-seed/idp_token.py alice@example.com 2>"$RUN_DIR/step7-token.err")"
  check "7 dex token" 0 "Dex issued an ID token for the claude-desktop client" test "$(tr -cd . <<<"$token" | wc -c | tr -d ' ')" = 2
  before="$(newest_id "$IDP_KEY_ID")"
  status="$(curl -s -o "$RUN_DIR/step7-body.json" -w '%{http_code}' "$PRELOOP/anthropic/v1/messages" \
    -H "authorization: Bearer $token" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -H 'x-preloop-client: claude-desktop' \
    -d '{"model":"claude-sonnet-4-5","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}')"
  check "7 served" 0 "IdP bearer answered $status from the stub model" \
    bash -c '[ "$1" = 200 ] && grep -q PRELOOP_STUB_OK "$2"' _ "$status" "$RUN_DIR/step7-body.json"
  row="$(wait_usage_after "$IDP_KEY_ID" "$before")"
  echo "$row" | jq . >"$RUN_DIR/step7-usage-row.json"
  meta="$(jq -c '.meta_data // {}' <<<"$row")"
  check "7 usage subject" 0 "usage row on the binding key: auth_method=$(jq -r .auth_method <<<"$meta"), subject=$(jq -r .gateway_subject_email <<<"$meta")" \
    bash -c '[ "$(jq -r .auth_method <<<"$1")" = idp ] && [ "$(jq -r .gateway_subject_email <<<"$1")" = alice@example.com ] && [ "$(jq -r .gateway_source <<<"$1")" = direct ]' _ "$meta"
  # Change the last signature character (A<->B) so the token always differs.
  if [ "${token: -1}" = A ]; then tampered="${token%?}B"; else tampered="${token%?}A"; fi
  status="$(curl -s -o "$RUN_DIR/step7-tampered.json" -D "$RUN_DIR/step7-tampered.headers" -w '%{http_code}' "$PRELOOP/anthropic/v1/messages" \
    -H "authorization: Bearer $tampered" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
    -d '{"model":"claude-sonnet-4-5","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}')"
  check "7 tampered" 0 "a token with a modified signature answered $status with WWW-Authenticate" \
    bash -c '[ "$1" = 401 ] && grep -qi "^www-authenticate: Bearer error=\"invalid_token\"" "$2"' _ "$status" "$RUN_DIR/step7-tampered.headers"
}

# ---------------------------------------------------------------- bring-up
log "# Claude apps gateway harness run $(date -u +%FT%TZ)"
log ""
UP_SERVICES=""
[ "${IDP_ONLY:-0}" = 1 ] && UP_SERVICES="preloop dex-idp stub-model"
# shellcheck disable=SC2086
DC up -d --build --wait $UP_SERVICES >"$RUN_DIR/compose-up.log" 2>&1 || {
  echo "compose up failed, see $RUN_DIR/compose-up.log" >&2; tail -30 "$RUN_DIR/compose-up.log" >&2; exit 2; }
SEED="$(DC exec -T -e PRELOOP_UPSTREAM_KEY -e PRELOOP_UPSTREAM_SECRET preloop python /harness-seed/seed.py 2>/dev/null | tail -1)"
IDP_KEY_ID="$(jq -r .idp_key_id <<<"$SEED")"
ADMIN_KEY="$(jq -r .admin_key <<<"$SEED")"
DIRECT_KEY="$(jq -r .direct_key <<<"$SEED")"
DIRECT_KEY_ID="$(jq -r .direct_key_id <<<"$SEED")"
TELEMETRY_KEY_ID="$(jq -r .telemetry_key_id <<<"$SEED")"
TRUSTED_KEY_ID="$(jq -r .trusted_key_id <<<"$SEED")"
[ -n "$ADMIN_KEY" ] && [ "$ADMIN_KEY" != null ] || { echo "seed failed: $SEED" >&2; exit 2; }

MODELS_STATUS="$(curl -s -o /dev/null -w '%{http_code}' "$PRELOOP/anthropic/v1/models" \
  -H "x-api-key: $DIRECT_KEY" -H 'anthropic-version: 2023-06-01')"
CONTRACT=0; [ "$MODELS_STATUS" = 200 ] && CONTRACT=1
GW_VERSION="not started (IDP_ONLY)"
[ "${IDP_ONLY:-0}" = 1 ] || GW_VERSION="$(CX 'claude --version' | head -1)"
DEX_IMAGE="$(DC config --images 2>/dev/null | grep dex | head -1)"
PRELOOP_COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
log "## Versions"
log "- Claude Code (gateway server and client): $GW_VERSION"
log "- Dex: ${DEX_IMAGE:-ghcr.io/dexidp/dex}"
log "- Preloop commit: $PRELOOP_COMMIT$(git diff --quiet 2>/dev/null || echo ' (dirty)')"
log "- Backend contract (#1409) detected: $([ $CONTRACT = 1 ] && echo yes || echo "no (GET /anthropic/v1/models -> $MODELS_STATUS)")"
log ""
log "## Steps"
if [ "${IDP_ONLY:-0}" = 1 ]; then
  idp_step
  log ""
  log "## Result: $PASS passed, $FAIL failed, $PENDING pending"
  [ "${KEEP_UP:-1}" = 0 ] && DC down -v >/dev/null 2>&1
  echo "report: $REPORT"
  [ "$FAIL" = 0 ] || exit 1
  exit 0
fi
check "0 stub tests" 0 "harness recorder unit tests (stub/test_harness_server.py)" \
  python3 stub/test_harness_server.py
curl -s -X DELETE "$RECORDER/_harness/requests" >/dev/null
curl -s -X DELETE "$SPARE/_harness/requests" >/dev/null
# Reruns: drop gateway_subject budgets left by an earlier run on this stack.
for id in $(api "/budget/policies" | jq -r '.[]? | select(.subject_type == "gateway_subject") | .id'); do
  api "/budget/policies/$id" -X DELETE >/dev/null
done

# ------------------------------------------- step 1: sign in (throwaway HOME)
HOME_A="/tmp/harness-home-alice-$$"; HOME_B="/tmp/harness-home-bob-$$"
for pair in "alice:$HOME_A" "bob:$HOME_B"; do
  user="${pair%%:*}"; home="${pair#*:}"
  CX "bash /harness-client/signin.sh $home $user@example.com password" >"$RUN_DIR/signin-$user.log" 2>&1
  STATUS="$(CX "HOME=$home claude auth status </dev/null" 2>/dev/null)"
  check "1 sign-in ($user)" 0 "forceLoginMethod=gateway + forceLoginGatewayUrl, /login device flow via Dex, auth status apiProvider=$(jq -r .apiProvider <<<"$STATUS" 2>/dev/null)" \
    test "$(jq -r '.loggedIn and .apiProvider == "gateway"' <<<"$STATUS" 2>/dev/null)" = true
done

claude_p() { # claude_p <home> <prompt> -> stdout+stderr of claude -p
  CX "cd $1 && HOME=$1 timeout 120 claude -p '$2' </dev/null 2>&1"
}

# ------------------------------------------------ step 2: allowed request
BEFORE="$(newest_id "$TRUSTED_KEY_ID")"
OUT_A="$(claude_p "$HOME_A" 'Reply with the word ok')"
echo "$OUT_A" >"$RUN_DIR/step2-claude-output.txt"
check "2 reply" 0 "claude -p answered from Preloop's stub model (output: $(head -c 80 <<<"$OUT_A"))" grep -q PRELOOP_STUB_OK <<<"$OUT_A"
ROW_A="$(wait_usage_after "$TRUSTED_KEY_ID" "$BEFORE")"
echo "$ROW_A" | jq . >"$RUN_DIR/step2-usage-row.json"
META_A="$(jq -c '.meta_data // {}' <<<"$ROW_A")"
check "2 usage row" 0 "usage row on the trusted upstream key, status $(jq -r .status_code <<<"$ROW_A")" \
  test "$(jq -r .status_code <<<"$ROW_A")" = 200
check "2 gateway_source" 1 "meta_data.gateway_source=$(jq -r .gateway_source <<<"$META_A")" \
  test "$(jq -r .gateway_source <<<"$META_A")" = claude_apps_gateway
check "2 subject email" 1 "meta_data.gateway_subject_email=$(jq -r .gateway_subject_email <<<"$META_A")" \
  test "$(jq -r .gateway_subject_email <<<"$META_A")" = alice@example.com
check "2 subject id" 1 "meta_data.gateway_subject_id=$(jq -r .gateway_subject_id <<<"$META_A")" \
  test "$(jq -r '.gateway_subject_id // "" | length > 0' <<<"$META_A")" = true
check "2 client" 1 "meta_data.client=$(jq -r .client <<<"$META_A")" \
  grep -qxE 'claude_code|claude_desktop' <<<"$(jq -r .client <<<"$META_A")"

# ---------------------------------------------- step 3: budget denial = 429
BEFORE="$(newest_id "$TRUSTED_KEY_ID")"
claude_p "$HOME_B" 'Reply with the word ok' >"$RUN_DIR/step3-bob-first.txt"
wait_usage_after "$TRUSTED_KEY_ID" "$BEFORE" >/dev/null
SUBJECT_B="$(api "/account/gateway-usage/search?api_key_id=$TRUSTED_KEY_ID&limit=50" | jq -r \
  '[.items[].meta_data | select(.gateway_subject_email == "bob@example.com") | .gateway_subject_id][0] // ""')"
if [ -n "$SUBJECT_B" ]; then
  api "/budget/policies" -X POST -H 'content-type: application/json' -d "$(jq -nc --arg s "$SUBJECT_B" \
    '{subject_type:"gateway_subject",subject_id:$s,period:"monthly",hard_limit_usd:0.000001}')" >"$RUN_DIR/step3-policy.json"
fi
curl -s -X DELETE "$SPARE/_harness/requests" >/dev/null
TOKEN_B="$(CX 'bash /harness-client/device-login.sh bob@example.com password 2>/dev/null' | jq -r .access_token)"
CX "curl -s -D - -o /tmp/step3-body.json http://localhost:8080/v1/messages -H 'authorization: Bearer $TOKEN_B' \
  -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{\"model\":\"claude-sonnet-4-5\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'; echo; cat /tmp/step3-body.json" >"$RUN_DIR/step3-raw-429.txt"
STATUS3="$(head -1 "$RUN_DIR/step3-raw-429.txt" | awk '{print $2}')"
RETRY3="$(grep -i '^retry-after:' "$RUN_DIR/step3-raw-429.txt" | awk '{print $2}' | tr -d '\r')"
BODY3="$(tail -1 "$RUN_DIR/step3-raw-429.txt")"
OUT_B2="$(claude_p "$HOME_B" 'Reply with the word ok')"
echo "$OUT_B2" >"$RUN_DIR/step3-claude-code-sees.txt"
SPARE_HITS="$(curl -s "$SPARE/_harness/requests" | jq '.requests | length')"
check "3 status" 1 "developer got HTTP $STATUS3 from the gateway" test "$STATUS3" = 429
check "3 retry-after" 1 "retry-after=${RETRY3:-missing} (integer)" grep -qxE '[0-9]+' <<<"${RETRY3:-x}"
check "3 billing_error" 1 "body error.type=$(jq -r '.error.type' <<<"$BODY3" 2>/dev/null)" \
  test "$(jq -r '.error.type' <<<"$BODY3" 2>/dev/null)" = billing_error
check "3 no failover" 1 "second upstream received $SPARE_HITS requests after the 429" \
  bash -c '[ "$1" = 429 ] && [ "$2" = 0 ]' _ "$STATUS3" "$SPARE_HITS"
log "  - Claude Code showed: \`$(tr '\n' ' ' <<<"$OUT_B2" | head -c 300)\`"

# ------------------------------- step 4: forged identity on a normal key
BEFORE="$(newest_id "$DIRECT_KEY_ID")"
STATUS4="$(curl -s -o "$RUN_DIR/step4-body.json" -w '%{http_code}' "$PRELOOP/anthropic/v1/messages" \
  -H "x-api-key: $DIRECT_KEY" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -H 'x-claude-gateway-user-id: forged-sub' -H 'x-claude-gateway-user-email: mallory@example.com' \
  -H 'x-litellm-end-user-id: mallory@example.com' \
  -d '{"model":"claude-sonnet-4-5","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}')"
ROW4="$(wait_usage_after "$DIRECT_KEY_ID" "$BEFORE")"
echo "$ROW4" | jq . >"$RUN_DIR/step4-usage-row.json"
check "4 served" 0 "normal key with forged identity headers answered $STATUS4" test "$STATUS4" = 200
check "4 not attributed" 0 "usage row does not name the forged identity" \
  bash -c '! grep -q "mallory@example.com\|forged-sub" <<<"$1"' _ "$(jq -c .meta_data <<<"$ROW4")"
check "4 direct source" 1 "meta_data.gateway_source=$(jq -r .meta_data.gateway_source <<<"$ROW4")" \
  test "$(jq -r .meta_data.gateway_source <<<"$ROW4")" = direct
STATUS4B="$(curl -s -o /dev/null -w '%{http_code}' "$PRELOOP/anthropic/v1/messages" \
  -H "x-api-key: $PRELOOP_UPSTREAM_KEY" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -H 'x-claude-gateway-user-id: forged-sub' -H 'x-preloop-upstream-secret: wrong' \
  -d '{"model":"claude-sonnet-4-5","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}')"
check "4 wrong secret" 1 "trusted key with a wrong x-preloop-upstream-secret answered $STATUS4B" test "$STATUS4B" = 401

# ------------------------------------------- step 5: header names observed
curl -s "$RECORDER/_harness/requests" >"$RUN_DIR/step5-recorder.json"
NAMES="$(jq -r '[.requests[] | select(.path | contains("/messages")) | .header_names[]] | unique | join(", ")' "$RUN_DIR/step5-recorder.json")"
UAS="$(jq -r '[.requests[].user_agent] | unique | join(" | ")' "$RUN_DIR/step5-recorder.json")"
PATHS="$(jq -r '[.requests[] | "\(.method) \(.path)"] | unique | join(", ")' "$RUN_DIR/step5-recorder.json")"
check "5 headers recorded" 0 "$(jq '.requests | length' "$RUN_DIR/step5-recorder.json") gateway requests recorded" test -n "$NAMES"
log "  - header names at Preloop: $NAMES"
log "  - x-claude-code-session-id forwarded: $(grep -q 'x-claude-code-session-id' <<<"$NAMES" && echo yes || echo no)"
log "  - user-agent forwarded: $(grep -qw 'user-agent' <<<"$NAMES" && echo "yes ($UAS)" || echo no)"
log "  - paths: $PATHS"

# What the gateway relayed during steps 2-3 (step 6 recreates the relay).
RELAYED="$(DC logs --no-log-prefix telemetry-relay 2>/dev/null | grep -o 'POST /api/v1/telemetry/otlp/v1/[a-z]* HTTP/1.1" [0-9]*' | sed 's/ HTTP\/1.1"//' | sort | uniq -c | awk '{printf "%s%s %s x%s", sep, $3, $4, $1; sep=", "}')"

idp_step

# ------------------------------------------------------- step 6: rollback
DC exec -T -e PRELOOP_UPSTREAM_KEY preloop python /harness-seed/seed.py --revoke-upstream >/dev/null 2>&1
curl -s -X DELETE "$SPARE/_harness/requests" >/dev/null
OUT6="$(claude_p "$HOME_A" 'Reply with the word ok')"
SPARE6="$(curl -s "$SPARE/_harness/requests" | jq '.requests | length')"
check "6 key revoked" 0 "Preloop answers 401 and the gateway fails over to the next upstream (spare hits $SPARE6, output: $(head -c 40 <<<"$OUT6"))" \
  grep -q SPARE_UPSTREAM_REPLY <<<"$OUT6"
DC exec -T -e PRELOOP_UPSTREAM_KEY preloop python /harness-seed/seed.py --restore-upstream >/dev/null 2>&1
curl -s -X DELETE "$RECORDER/_harness/requests" >/dev/null
GATEWAY_CONFIG_FILE=gateway.rollback.yaml DC up -d --wait --force-recreate --no-deps claude-gateway >>"$RUN_DIR/compose-up.log" 2>&1
DC up -d --wait --force-recreate --no-deps client telemetry-relay >>"$RUN_DIR/compose-up.log" 2>&1
HOME_R="/tmp/harness-home-rollback-$$"
CX "bash /harness-client/signin.sh $HOME_R alice@example.com password" >"$RUN_DIR/signin-rollback.log" 2>&1
OUT6B="$(claude_p "$HOME_R" 'Reply with the word ok')"
PRELOOP6="$(curl -s "$RECORDER/_harness/requests" | jq '.requests | length')"
check "6 upstream removed" 0 "gateway without the Preloop upstream serves from the remaining upstream; Preloop received $PRELOOP6 requests" \
  bash -c 'grep -q SPARE_UPSTREAM_REPLY <<<"$1" && [ "$2" = 0 ]' _ "$OUT6B" "$PRELOOP6"
DC up -d --wait --force-recreate --no-deps claude-gateway >>"$RUN_DIR/compose-up.log" 2>&1
DC up -d --wait --force-recreate --no-deps client telemetry-relay >>"$RUN_DIR/compose-up.log" 2>&1

# ------------------------------------ step 8: OTLP telemetry ingest (#1412)
OTLP="$(DC exec -T -e PRELOOP_TELEMETRY_KEY -e DIRECT_KEY="$DIRECT_KEY" preloop \
  python /harness-seed/otlp_check.py 2>"$RUN_DIR/step8-otlp-check.err" | tail -1)"
echo "$OTLP" | jq . >"$RUN_DIR/step8-otlp-check.json" 2>/dev/null
otlp_true() { test "$(jq -r ".$1" <<<"$OTLP" 2>/dev/null)" = true; }
check "8 json export" 0 "OTLP JSON logs export answered $(jq -r .json_status <<<"$OTLP")" \
  test "$(jq -r .json_status <<<"$OTLP")" = 200
check "8 protobuf export" 0 "OTLP protobuf logs export answered $(jq -r .protobuf_status <<<"$OTLP")" \
  test "$(jq -r .protobuf_status <<<"$OTLP")" = 200
check "8 enrichment" 0 "api_request matching a gateway row's x-client-request-id enriches meta_data.telemetry" otlp_true enriched
check "8 enrichment no row" 0 "a matched api_request creates no usage row" otlp_true enrich_created_no_row
check "8 estimate row" 0 "an unmatched api_request creates one telemetry_estimate row" otlp_true estimate_row
check "8 replay" 0 "re-sending the same export creates nothing" otlp_true replay_noop
check "8 late gateway row" 0 "a gateway row after the telemetry leaves exactly one row" otlp_true late_gateway_one_row
check "8 privacy" 0 "the prompt attribute is not stored" otlp_true prompt_not_stored
log "  - exports the apps gateway relayed to Preloop (path status count): ${RELAYED:-none}"
# Steps 2-3 ran real Claude Code sessions through the gateway with logs and
# metrics relayed to Preloop: none of that telemetry may double count.
RELAY="$(DC exec -T -e PRELOOP_TELEMETRY_KEY -e DIRECT_KEY="$DIRECT_KEY" preloop \
  python /harness-seed/otlp_check.py --relayed 2>>"$RUN_DIR/step8-otlp-check.err" | tail -1)"
echo "$RELAY" | jq . >"$RUN_DIR/step8-relayed.json" 2>/dev/null
check "8 relayed telemetry" 0 "relayed exports reached Preloop: $(jq -r .telemetry_records <<<"$RELAY") records" \
  test "$(jq -r '.telemetry_records > 0' <<<"$RELAY" 2>/dev/null)" = true
check "8 relayed no double count" 0 "apps gateway rows $(jq -r .apps_gateway_rows <<<"$RELAY"), enriched $(jq -r .enriched_gateway_rows <<<"$RELAY"), duplicate telemetry rows $(jq -r .duplicate_rows <<<"$RELAY"), aggregates next to logs or gateway rows $(jq -r .overlapping_aggregates <<<"$RELAY")" \
  test "$(jq -r '.duplicate_rows == 0 and .overlapping_aggregates == 0' <<<"$RELAY" 2>/dev/null)" = true

# ----------------------------------------------------------------- summary
log ""
log "## Result: $PASS passed, $FAIL failed, $PENDING pending"
[ "${KEEP_UP:-1}" = 0 ] && DC down -v >/dev/null 2>&1
echo "report: $REPORT"
[ "$FAIL" = 0 ] || exit 1
[ "${STRICT:-0}" = 1 ] && [ "$PENDING" != 0 ] && exit 1
exit 0
