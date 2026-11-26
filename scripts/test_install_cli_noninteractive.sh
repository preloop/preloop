#!/usr/bin/env sh
# Offline checks of the installer's post-install discovery and prompt paths.
set -eu
export PRELOOP_DISABLE_TELEMETRY=true
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT INT TERM
mkdir -p "$TEST_ROOT/home/.cursor" "$TEST_ROOT/bin"
printf '%s\n' '{"mcpServers":{}}' > "$TEST_ROOT/home/.cursor/mcp.json"
export HOME="$TEST_ROOT/home"
export PRELOOP_TEST_LOG="$TEST_ROOT/calls"
export PRELOOP_TEST_HOME="$HOME"
cat > "$TEST_ROOT/bin/preloop" <<'CLI'
#!/usr/bin/env sh
printf '%s\n' "$*" >> "$PRELOOP_TEST_LOG"
case "$*" in
  'agents discover --no-onboard-prompt') echo 'Found 1 AI agent(s):' ;;
  'auth status') printf 'Authenticated\n  User: Jane Doe\n' ;;
  *) echo changed > "$PRELOOP_TEST_HOME/unexpected-mutation" ;;
esac
CLI
chmod +x "$TEST_ROOT/bin/preloop"
# Exercise the real post-install block, after the authorized binary install.
# Its only external command is the fixture CLI, which marks any mutation.
awk '/^# Step 1: discover/ { printing=1 } printing { print }' "$SCRIPT_DIR/install-cli.sh" > "$TEST_ROOT/post-install.sh"
awk '/^preloop_confirm_set\(\)/ { printing=1 } printing { print } printing && /^}/ { exit }' "$SCRIPT_DIR/install-cli.sh" > "$TEST_ROOT/helpers.sh"
awk '/^prompt_default_yes\(\)/ { printing=1 } printing { print } printing && /^}/ { exit }' "$SCRIPT_DIR/install-cli.sh" >> "$TEST_ROOT/helpers.sh"
cat > "$TEST_ROOT/run.sh" <<'RUN'
set -eu
. "$1/helpers.sh"
PRELOOP_BIN="$1/bin/preloop"
. "$1/post-install.sh"
RUN
cp -R "$HOME" "$TEST_ROOT/before"
unset PRELOOP_CONFIRM PRELOOP_URL
for mode in closed null pipe script-pipe; do
  : > "$PRELOOP_TEST_LOG"
  case "$mode" in
    closed) sh "$TEST_ROOT/run.sh" "$TEST_ROOT" <&- > "$TEST_ROOT/output" ;;
    null) sh "$TEST_ROOT/run.sh" "$TEST_ROOT" < /dev/null > "$TEST_ROOT/output" ;;
    pipe) printf 'y\ny\ny\n' | sh "$TEST_ROOT/run.sh" "$TEST_ROOT" > "$TEST_ROOT/output" ;;
    script-pipe) cat "$TEST_ROOT/run.sh" | sh -s -- "$TEST_ROOT" > "$TEST_ROOT/output" ;;
  esac
  grep -q 'Onboarding skipped' "$TEST_ROOT/output"
  [ "$(cat "$PRELOOP_TEST_LOG")" = 'agents discover --no-onboard-prompt' ]
  diff -r "$TEST_ROOT/before" "$HOME"
  echo "PASS post-install $mode leaves fixture home unchanged"
done
for mode in closed pipe; do
  if [ "$mode" = closed ]; then
    answer="$(sh -c '. "$1/helpers.sh"; prompt_default_yes "Apply?"' sh "$TEST_ROOT" <&-)"
  else
    answer="$(printf 'y\n' | sh -c '. "$1/helpers.sh"; prompt_default_yes "Apply?"' sh "$TEST_ROOT")"
  fi
  [ "$answer" = n ]
  echo "PASS default-yes prompt rejects $mode stdin"
done
# An explicit opt-in still reaches the mutating command without /dev/tty.
: > "$PRELOOP_TEST_LOG"
PRELOOP_CONFIRM=1 sh "$TEST_ROOT/run.sh" "$TEST_ROOT" <&- > "$TEST_ROOT/output"
grep -q '^agents onboard$' "$PRELOOP_TEST_LOG"
[ -f "$HOME/unexpected-mutation" ]
echo 'PASS PRELOOP_CONFIRM explicitly enables unattended onboarding'
