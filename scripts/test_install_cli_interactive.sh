#!/usr/bin/env sh
# Checks of the installer's interactive path: a person at a terminal answers
# one question (sign in), and the installer then runs, in this order,
#   preloop agents discover --brief --no-onboard-prompt
#   preloop login --no-runner
#   preloop agents onboard
# The post-install block runs in a pseudo-terminal against a fixture CLI
# that only records how it was called. Nothing is downloaded or installed.
set -eu
export PRELOOP_DISABLE_TELEMETRY=true
if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP: python3 is needed to open a pseudo-terminal"
  exit 0
fi
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT INT TERM
mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/bin"
export HOME="$TEST_ROOT/home"
export PRELOOP_TEST_LOG="$TEST_ROOT/calls"
cat > "$TEST_ROOT/bin/preloop" <<'CLI'
#!/usr/bin/env sh
printf '%s | PRELOOP_URL=%s\n' "$*" "${PRELOOP_URL:-}" >> "$PRELOOP_TEST_LOG"
case "$*" in
  'agents discover --brief --no-onboard-prompt')
    printf 'Found 1 AI agent(s):\n  Cursor  ~/.cursor/mcp.json  (not onboarded)\n' ;;
  'auth status')
    if [ "${FIXTURE_SIGNED_IN:-0}" = 1 ]; then
      printf 'Authenticated\n  User: Jane Doe\n'
    else
      echo 'Not authenticated'
    fi ;;
  'login --no-runner'|'signup --no-runner') echo 'Successfully authenticated!' ;;
  'agents onboard') echo 'Apply to 1 agent? [Y/n/c/e]: (fixture)' ;;
  *) echo "unexpected call: $*"; exit 9 ;;
esac
CLI
chmod +x "$TEST_ROOT/bin/preloop"
awk '/^# Step 1: discover/ { printing=1 } printing { print }' "$SCRIPT_DIR/install-cli.sh" > "$TEST_ROOT/post-install.sh"
awk '/^preloop_confirm_set\(\)/ { printing=1 } printing { print } printing && /^}/ { exit }' "$SCRIPT_DIR/install-cli.sh" > "$TEST_ROOT/helpers.sh"
awk '/^prompt_default_yes\(\)/ { printing=1 } printing { print } printing && /^}/ { exit }' "$SCRIPT_DIR/install-cli.sh" >> "$TEST_ROOT/helpers.sh"
cat > "$TEST_ROOT/run.sh" <<'RUN'
set -eu
. "$1/helpers.sh"
PRELOOP_BIN="$1/bin/preloop"
. "$1/post-install.sh"
RUN
# drive.py <answers...>: run the block in a pseudo-terminal and type one
# answer each time the output stops at a prompt (a line ending in ":").
cat > "$TEST_ROOT/drive.py" <<'PY'
import os, pty, select, sys, time

answers = sys.argv[2:]
pid, master = pty.fork()
if pid == 0:
    os.execvp("sh", ["sh", sys.argv[1] + "/run.sh", sys.argv[1]])
output = b""
typed = 0
last = time.monotonic()
deadline = last + 60
while time.monotonic() < deadline:
    ready, _, _ = select.select([master], [], [], 0.1)
    if ready:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        last = time.monotonic()
        continue
    tail = output.decode("utf-8", "replace").replace("\r", "").rsplit("\n", 1)[-1]
    if tail.rstrip().endswith(":") and time.monotonic() - last > 0.4:
        if typed >= len(answers):
            break
        os.write(master, (answers[typed] + "\n").encode())
        typed += 1
        last = time.monotonic()
_, status = os.waitpid(pid, 0)
sys.stdout.write(output.decode("utf-8", "replace").replace("\r", ""))
sys.stdout.write("\n[prompts answered: %d]\n" % typed)
sys.exit(os.waitstatus_to_exitcode(status))
PY

calls() { sed -e 's/ | PRELOOP_URL=.*$//' "$PRELOOP_TEST_LOG"; }
fail() { echo "FAIL: $*" >&2; echo "--- output"; cat "$TEST_ROOT/output"; echo "--- calls"; cat "$PRELOOP_TEST_LOG"; exit 1; }

unset PRELOOP_CONFIRM PRELOOP_URL FIXTURE_SIGNED_IN

# 1. Enter at the sign-in question: sign in, then the review. A second Enter
#    is offered for the review, which the fixture CLI does not read.
: > "$PRELOOP_TEST_LOG"
python3 "$TEST_ROOT/drive.py" "$TEST_ROOT" "" "" > "$TEST_ROOT/output" 2>&1 || fail "installer exited non-zero"
expected='agents discover --brief --no-onboard-prompt
auth status
login --no-runner
agents onboard'
[ "$(calls)" = "$expected" ] || fail "commands were not run in the expected order"
grep -q 'Sign in to preloop.ai to set up these agents? \[Y\] sign in, \[s\] sign up, \[u\] use a self-hosted URL, \[n\] not now:' "$TEST_ROOT/output" || fail "the folded sign-in question is missing"
grep -q '^\[prompts answered: 1\]$' "$TEST_ROOT/output" || fail "the installer asked more or fewer than one question"
if grep -q 'Onboard discovered agents now' "$TEST_ROOT/output" || grep -q 'Preloop instance URL \[' "$TEST_ROOT/output"; then
  fail "a removed question is still asked"
fi
echo "PASS Enter signs in and opens the review: one installer question"

# 2. "u" asks for the URL, then signs in against it.
: > "$PRELOOP_TEST_LOG"
python3 "$TEST_ROOT/drive.py" "$TEST_ROOT" "u" "http://localhost:8000" > "$TEST_ROOT/output" 2>&1 || fail "installer exited non-zero"
grep -q '^login --no-runner | PRELOOP_URL=http://localhost:8000$' "$PRELOOP_TEST_LOG" || fail "login did not target the self-hosted URL"
grep -q '^agents onboard | PRELOOP_URL=http://localhost:8000$' "$PRELOOP_TEST_LOG" || fail "onboarding did not target the self-hosted URL"
echo "PASS u asks for a self-hosted URL and uses it"

# 3. "s" signs up; "n" does nothing more.
: > "$PRELOOP_TEST_LOG"
python3 "$TEST_ROOT/drive.py" "$TEST_ROOT" "s" > "$TEST_ROOT/output" 2>&1 || fail "installer exited non-zero"
calls | grep -q '^signup --no-runner$' || fail "s did not run signup"
: > "$PRELOOP_TEST_LOG"
python3 "$TEST_ROOT/drive.py" "$TEST_ROOT" "n" > "$TEST_ROOT/output" 2>&1 || fail "installer exited non-zero"
if calls | grep -q -e '^login' -e '^signup' -e '^agents onboard'; then
  fail "n still signed in or onboarded"
fi
grep -q 'Skipped authentication' "$TEST_ROOT/output" || fail "n did not say how to continue later"
echo "PASS s signs up and n stops"

# 4. PRELOOP_URL decides the instance: the question has no "u".
: > "$PRELOOP_TEST_LOG"
PRELOOP_URL=https://preloop.example.com python3 "$TEST_ROOT/drive.py" "$TEST_ROOT" "" > "$TEST_ROOT/output" 2>&1 || fail "installer exited non-zero"
grep -q 'Sign in to preloop.example.com to set up these agents? \[Y\] sign in, \[s\] sign up, \[n\] not now:' "$TEST_ROOT/output" || fail "PRELOOP_URL did not shape the question"
grep -q '^login --no-runner | PRELOOP_URL=https://preloop.example.com$' "$PRELOOP_TEST_LOG" || fail "PRELOOP_URL was not passed on"
echo "PASS PRELOOP_URL skips the question about the URL"

# 5. Already signed in: no installer question at all, straight to the review.
: > "$PRELOOP_TEST_LOG"
FIXTURE_SIGNED_IN=1 python3 "$TEST_ROOT/drive.py" "$TEST_ROOT" > "$TEST_ROOT/output" 2>&1 || fail "installer exited non-zero"
expected='agents discover --brief --no-onboard-prompt
auth status
agents onboard'
[ "$(calls)" = "$expected" ] || fail "a signed-in machine did not go straight to the review"
grep -q '^\[prompts answered: 0\]$' "$TEST_ROOT/output" || fail "a signed-in machine was asked an installer question"
echo "PASS a signed-in machine goes straight to the review"
