#!/usr/bin/env bash
# Route D check: send a Claude-Desktop-shaped request straight to Preloop
# (Desktop `inferenceProvider: gateway`, no apps gateway in between) and
# assert the usage row is attributed to claude_desktop.
# Test-only; needs the harness stack (`./verify.sh` or `docker compose up`).
# It never touches Claude Desktop's managed configuration on this machine.
set -uo pipefail
export PRELOOP_DISABLE_TELEMETRY=true
cd "$(dirname "$0")" || exit 2
[ -f .env ] || cp .env.example .env
set -a; . ./.env; set +a
PRELOOP="http://127.0.0.1:${HARNESS_PRELOOP_PORT:-18900}"
SEED="$(docker compose exec -T -e PRELOOP_UPSTREAM_KEY -e PRELOOP_UPSTREAM_SECRET preloop python /harness-seed/seed.py 2>/dev/null | tail -1)"
ADMIN_KEY="$(jq -r .admin_key <<<"$SEED")"
KEY="$(jq -r .direct_key <<<"$SEED")"
KEY_ID="$(jq -r .direct_key_id <<<"$SEED")"
FAIL=0
ok() { echo "PASS $*"; }
bad() { echo "FAIL $*"; FAIL=1; }
newest() {
  curl -s -H "Authorization: Bearer $ADMIN_KEY" \
    "$PRELOOP/api/v1/account/gateway-usage/search?api_key_id=$KEY_ID&limit=1" | jq -c '.items[0] // {}'
}

# Desktop appends /v1/messages to inferenceGatewayBaseUrl. Probe both base
# URL forms an admin might enter and report which one Preloop serves.
BODY='{"model":"claude-sonnet-4-5","max_tokens":64,"stream":true,
  "system":[{"type":"text","text":"You are a test.","cache_control":{"type":"ephemeral"}}],
  "messages":[{"role":"user","content":[{"type":"text","text":"hi","cache_control":{"type":"ephemeral"}}]}]}'
desktop_post() { # desktop_post <base_url> <out_file> -> http status
  curl -sN -o "$2" -w '%{http_code}' "$1/v1/messages" \
    -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' \
    -H 'anthropic-beta: prompt-caching-2024-07-31' \
    -H 'content-type: application/json' -H 'accept: text/event-stream' \
    -H 'X-Preloop-Client: claude-desktop' \
    -H "x-claude-code-session-id: desktop-harness-$(date +%s)" \
    -d "$BODY"
}
BEFORE="$(newest | jq -r '.api_usage_id // ""')"
S_ANTHROPIC="$(desktop_post "$PRELOOP/anthropic" /tmp/desktop-a.txt)"
S_V1="$(desktop_post "$PRELOOP/anthropic/v1" /tmp/desktop-b.txt)"
echo "base URL $PRELOOP/anthropic    -> POST .../anthropic/v1/messages    HTTP $S_ANTHROPIC"
echo "base URL $PRELOOP/anthropic/v1 -> POST .../anthropic/v1/v1/messages HTTP $S_V1"
[ "$S_ANTHROPIC" = 200 ] && ok "base URL form <preloop>/anthropic serves Desktop" || bad "base URL <preloop>/anthropic -> $S_ANTHROPIC"
grep -q 'event: message_stop' /tmp/desktop-a.txt && ok "streamed SSE ends with message_stop" || bad "stream incomplete"

ROW=""
for _ in $(seq 1 20); do
  ROW="$(newest)"
  [ "$(jq -r '.api_usage_id // ""' <<<"$ROW")" != "$BEFORE" ] && break
  sleep 1
done
CLIENT="$(jq -r '.meta_data.client // "missing"' <<<"$ROW")"
SOURCE="$(jq -r '.meta_data.gateway_source // "missing"' <<<"$ROW")"
[ "$CLIENT" = claude_desktop ] && ok "usage meta_data.client=claude_desktop" || bad "usage meta_data.client=$CLIENT (expects #1409)"
[ "$SOURCE" = direct ] && ok "usage meta_data.gateway_source=direct" || bad "usage meta_data.gateway_source=$SOURCE (expects #1409)"

S_MODELS="$(curl -s -o /tmp/desktop-models.json -w '%{http_code}' "$PRELOOP/anthropic/v1/models" \
  -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' -H 'X-Preloop-Client: claude-desktop')"
[ "$S_MODELS" = 200 ] && ok "GET <base>/v1/models (Desktop model picker) -> 200, $(jq '.data | length' /tmp/desktop-models.json) models" \
  || bad "GET <base>/v1/models -> $S_MODELS (expects #1409)"
exit "$FAIL"
