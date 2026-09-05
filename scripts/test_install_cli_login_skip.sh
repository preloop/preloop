#!/usr/bin/env sh
# Unit tests for existing_login_identity in install-cli.sh — the check that
# lets a re-run of the installer skip the login prompt when the machine
# already has a verified session.

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INSTALLER="${SCRIPT_DIR}/install-cli.sh"
TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT INT TERM

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "ok - $*"
}

HELPER="${TMP_DIR}/existing_login_identity.sh"
awk '
  /^existing_login_identity\(\)/ { printing=1 }
  printing { print }
  printing && /^}/ { exit }
' "$INSTALLER" > "$HELPER"
[ -s "$HELPER" ] || fail "could not extract existing_login_identity from $INSTALLER"

# Fake `preloop` whose `auth status` prints whatever FAKE_STATUS holds.
FAKE_CLI="${TMP_DIR}/preloop"
cat > "$FAKE_CLI" <<'EOF'
#!/usr/bin/env sh
printf '%s\n' "${FAKE_STATUS:-}"
exit 0
EOF
chmod +x "$FAKE_CLI"

cat > "${TMP_DIR}/run.sh" <<'EOF'
#!/usr/bin/env sh
set -eu
# shellcheck disable=SC1090
. "$1"
existing_login_identity "$2"
EOF
chmod +x "${TMP_DIR}/run.sh"

run_case() {
  name="$1"
  expect_status="$2"
  expect_substr="$3"
  status=0
  got="$(FAKE_STATUS="$FAKE_STATUS" sh "${TMP_DIR}/run.sh" "$HELPER" "$FAKE_CLI")" || status=$?

  if [ "$status" != "$expect_status" ]; then
    fail "$name: expected exit $expect_status, got $status (output: '$got')"
  fi
  if [ -n "$expect_substr" ]; then
    case "$got" in
      *"$expect_substr"*) ;;
      *) fail "$name: expected output to contain '$expect_substr', got '$got'" ;;
    esac
  fi
  pass "$name"
}

FAKE_STATUS='Authenticated
  User:    Ada Lovelace
  Email:   ada@example.com
  Org:     Analytical
  API URL: https://preloop.ai' \
  run_case "verified login is detected and identity echoed" 0 "ada@example.com"

FAKE_STATUS='Not authenticated
Run '\''preloop login --token <your-token>'\'' to authenticate' \
  run_case "no session falls through to login" 1 ""

# The degraded verdicts still start with "Authenticated": they mean the CLI
# could not confirm the session with the server, so the installer must NOT
# skip the login prompt.
FAKE_STATUS='Authenticated (stored login could not be refreshed)
  API URL: https://preloop.ai' \
  run_case "unrefreshable login falls through to login" 1 ""

FAKE_STATUS='Authenticated (token may be invalid or server unreachable)
  API URL: https://preloop.ai' \
  run_case "unverifiable token falls through to login" 1 ""

FAKE_STATUS='' \
  run_case "empty status output falls through to login" 1 ""

echo "All existing_login_identity tests passed."
