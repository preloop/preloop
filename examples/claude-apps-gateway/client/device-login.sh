#!/usr/bin/env bash
# Scripted gateway device sign-in (RFC 8628) for one Dex test user.
# Runs inside the `client` container. Plays the "browser" leg with curl
# against the gateway's /device page and Dex's local password form, then
# polls the token endpoint exactly like Claude Code's /login does.
# Usage: device-login.sh <email> <password> [user_code]
#   Without user_code: starts its own device grant and prints the token JSON.
#   With user_code (shown by Claude Code's /login): completes only the browser
#   leg for that code; Claude Code itself polls for the token.
set -euo pipefail
EMAIL="$1"
PASSWORD="$2"
GW="${GATEWAY_URL:-http://localhost:8080}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

USER_CODE="${3:-}"
DEVICE_CODE=""
if [ -z "$USER_CODE" ]; then
  DA="$(curl -sf -X POST "$GW/oauth/device_authorization")"
  USER_CODE="$(jq -r .user_code <<<"$DA")"
  DEVICE_CODE="$(jq -r .device_code <<<"$DA")"
fi

curl -sf -c "$JAR" -b "$JAR" -o /dev/null "$GW/device?user_code=$USER_CODE"
# The gateway's /device form is same-origin protected; send what a browser sends.
IDP_URL="$(curl -s -c "$JAR" -b "$JAR" -o /dev/null -w '%{redirect_url}' \
  -X POST "$GW/device" \
  -H "Origin: $GW" -H "Referer: $GW/device?user_code=$USER_CODE" \
  -H "Sec-Fetch-Site: same-origin" \
  --data-urlencode "user_code=$USER_CODE")"
[ -n "$IDP_URL" ] || { echo "device confirm did not redirect to the IdP" >&2; exit 1; }

# Dex: follow to the local password form, then post the credentials.
LOGIN_URL="$(curl -s -c "$JAR" -b "$JAR" -L -o /dev/null -w '%{url_effective}' "$IDP_URL")"
FINAL="$(curl -s -c "$JAR" -b "$JAR" -L -o /tmp/device-final.html -w '%{url_effective} %{http_code}' \
  -X POST "$LOGIN_URL" \
  --data-urlencode "login=$EMAIL" --data-urlencode "password=$PASSWORD")"
echo "browser leg ended at: ${FINAL%%\?*}" >&2
[ -n "$DEVICE_CODE" ] || exit 0

for _ in $(seq 1 30); do
  TOKEN="$(curl -s -X POST "$GW/oauth/token" \
    --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
    --data-urlencode "device_code=$DEVICE_CODE")"
  if jq -e .access_token >/dev/null 2>&1 <<<"$TOKEN"; then
    echo "$TOKEN"
    exit 0
  fi
  sleep 2
done
echo "token polling failed: $TOKEN" >&2
exit 1
