#!/usr/bin/env sh
# Unit tests for detect_arch in install-cli.sh.

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

DETECT_HELPER="${TMP_DIR}/detect_arch.sh"
awk '
  /^detect_arch\(\)/ { printing=1 }
  printing { print }
  printing && /^}/ { exit }
' "$INSTALLER" > "$DETECT_HELPER"

# Runner script avoids defining case/esac with ')' inside $(...), which breaks
# POSIX command substitution parsing.
cat > "${TMP_DIR}/run_detect.sh" <<'EOF'
#!/usr/bin/env sh
set -eu
unset PROCESSOR_ARCHITECTURE PROCESSOR_ARCHITEW6432 || true
# shellcheck disable=SC1090
. "$1"
# shellcheck disable=SC1090
. "$2"
uname() {
  case "$1" in
    -m) printf '%s\n' "${FAKE_UNAME_M:-x86_64}" ;;
    -s) printf '%s\n' "${FAKE_UNAME_S:-Linux}" ;;
    *) command uname "$@" ;;
  esac
}
detect_arch
EOF
chmod +x "${TMP_DIR}/run_detect.sh"

run_case() {
  name="$1"
  expect="$2"
  shift 2
  env_file="${TMP_DIR}/env.sh"
  : > "$env_file"
  for assignment in "$@"; do
    printf 'export %s\n' "$assignment" >> "$env_file"
  done

  status=0
  got="$(
    FAKE_UNAME_M="${FAKE_UNAME_M-}" FAKE_UNAME_S="${FAKE_UNAME_S-}" \
      sh "${TMP_DIR}/run_detect.sh" "$env_file" "$DETECT_HELPER"
  )" || status=$?

  if [ "$expect" = "ERROR" ]; then
    if [ "$status" -ne 0 ]; then
      pass "$name (exited $status)"
      return
    fi
    fail "$name: expected failure, got '$got'"
  fi
  if [ "$status" -ne 0 ]; then
    fail "$name: detect_arch exited $status"
  fi
  if [ "$got" != "$expect" ]; then
    fail "$name: expected '$expect', got '$got'"
  fi
  pass "$name"
}

FAKE_UNAME_M=x86_64 FAKE_UNAME_S=Linux \
  run_case "linux amd64 via uname" amd64

FAKE_UNAME_M=aarch64 FAKE_UNAME_S=Linux \
  run_case "linux arm64 via uname" arm64

FAKE_UNAME_M=arm64 FAKE_UNAME_S=Darwin \
  run_case "darwin arm64 via uname" arm64

FAKE_UNAME_M=i686 FAKE_UNAME_S=MINGW64_NT-10.0 \
  run_case "git bash i686 on amd64 windows" amd64 \
  PROCESSOR_ARCHITECTURE=x86 PROCESSOR_ARCHITEW6432=AMD64

FAKE_UNAME_M=i686 FAKE_UNAME_S=MINGW64_NT-10.0 \
  run_case "git bash i686 fallback without PROCESSOR_*" amd64

FAKE_UNAME_M=x86_64 FAKE_UNAME_S=MINGW64_NT-10.0 \
  run_case "windows amd64 via PROCESSOR_ARCHITECTURE" amd64 \
  PROCESSOR_ARCHITECTURE=AMD64

FAKE_UNAME_M=aarch64 FAKE_UNAME_S=MINGW64_NT-10.0 \
  run_case "windows arm64 via PROCESSOR_ARCHITECTURE" arm64 \
  PROCESSOR_ARCHITECTURE=ARM64

FAKE_UNAME_M=i686 FAKE_UNAME_S=MINGW32_NT-10.0 \
  run_case "true 32-bit windows errors" ERROR \
  PROCESSOR_ARCHITECTURE=x86

echo "All detect_arch tests passed."
