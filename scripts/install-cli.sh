#!/usr/bin/env sh

set -eu

PRELOOP_REPO="${PRELOOP_REPO:-preloop/preloop}"
INSTALL_DIR="${INSTALL_DIR:-}"
PRELOOP_DEFAULT_VERSION="${PRELOOP_DEFAULT_VERSION:-}"
PRELOOP_VERSION="${PRELOOP_VERSION:-$PRELOOP_DEFAULT_VERSION}"

# When PRELOOP_CONFIRM is set to a truthy value, every interactive prompt in
# this script (and downstream `preloop` commands invoked from it) is treated
# as if the user accepted the default. This makes the installer suitable for
# unattended automation (CI, Dockerfiles, configuration management, etc.).
preloop_confirm_set() {
  case "$(printf '%s' "${PRELOOP_CONFIRM:-}" | tr '[:upper:]' '[:lower:]')" in
    1|y|yes|true|on) return 0 ;;
    *) return 1 ;;
  esac
}

detect_os() {
  case "$(uname -s)" in
    Linux) echo "linux" ;;
    Darwin) echo "darwin" ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT) echo "windows" ;;
    *)
      echo "Unsupported operating system: $(uname -s)" >&2
      exit 1
      ;;
  esac
}

# detect_arch maps the host CPU to a release asset arch (amd64|arm64).
#
# On Windows, 32-bit Git Bash / MSYS reports `uname -m` as i686 even on
# 64-bit machines. Prefer PROCESSOR_ARCHITEW6432 / PROCESSOR_ARCHITECTURE
# (set by Windows) so those shells install the correct amd64/arm64 binary.
detect_arch() {
  machine="$(uname -m 2>/dev/null || true)"
  wow64="$(printf '%s' "${PROCESSOR_ARCHITEW6432:-}" | tr '[:upper:]' '[:lower:]')"
  proc="$(printf '%s' "${PROCESSOR_ARCHITECTURE:-}" | tr '[:upper:]' '[:lower:]')"

  case "$wow64" in
    amd64) echo "amd64"; return ;;
    arm64) echo "arm64"; return ;;
  esac

  case "$proc" in
    amd64|x86_64) echo "amd64"; return ;;
    arm64|aarch64) echo "arm64"; return ;;
    x86)
      echo "Unsupported architecture: 32-bit Windows (x86)." >&2
      echo "Preloop ships 64-bit Windows builds only. On 64-bit Windows, use" >&2
      echo "PowerShell: irm https://preloop.ai/install/cli.ps1 | iex" >&2
      exit 1
      ;;
  esac

  case "$machine" in
    x86_64|amd64) echo "amd64" ;;
    arm64|aarch64) echo "arm64" ;;
    i386|i686)
      # Common false report from 32-bit Git Bash on 64-bit Windows when
      # PROCESSOR_* env vars are unavailable. Prefer amd64 there; true
      # 32-bit hosts should have been caught via PROCESSOR_ARCHITECTURE=x86.
      os_name="$(uname -s 2>/dev/null || true)"
      case "$os_name" in
        MINGW*|MSYS*|CYGWIN*|Windows_NT)
          echo "amd64"
          return
          ;;
      esac
      echo "Unsupported architecture: ${machine}" >&2
      exit 1
      ;;
    *)
      echo "Unsupported architecture: ${machine:-unknown}" >&2
      exit 1
      ;;
  esac
}

resolve_version() {
  if [ -n "$PRELOOP_VERSION" ]; then
    echo "$PRELOOP_VERSION"
    return
  fi

  latest_json="$(curl -fsSL "https://api.github.com/repos/${PRELOOP_REPO}/releases/latest")"
  version="$(printf '%s' "$latest_json" | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  if [ -z "$version" ]; then
    echo "Could not determine the latest Preloop release" >&2
    exit 1
  fi
  echo "${version#v}"
}

resolve_install_dir() {
  if [ -n "$INSTALL_DIR" ]; then
    echo "$INSTALL_DIR"
    return
  fi

  # Prefer a user-local Windows path that does not require elevation.
  if [ "${OS:-}" = "windows" ] && [ -n "${LOCALAPPDATA:-}" ]; then
    echo "${LOCALAPPDATA}/Preloop/bin"
    return
  fi

  if [ -w "/usr/local/bin" ]; then
    echo "/usr/local/bin"
  else
    echo "${HOME}/.local/bin"
  fi
}

# prompt_default_yes prompts the user with a [Y/n] style question and writes
# the answer (lower-cased, with empty input treated as "y") to stdout.
# Callers invoke this inside a command substitution, which captures stdout,
# so the prompt itself MUST go to stderr: otherwise the user sees nothing
# while `read` blocks and the captured value would contain the prompt text.
# When PRELOOP_CONFIRM is set, the prompt is skipped and "y" is echoed.
prompt_default_yes() {
  prompt_text="$1"
  if preloop_confirm_set; then
    printf '%s y (PRELOOP_CONFIRM)\n' "$prompt_text" >&2
    echo "y"
    return
  fi
  printf '%s ' "$prompt_text" >&2
  # 2>/dev/null must come first so the shell's "cannot open /dev/tty" error
  # is silenced when there is no controlling terminal.
  if read -r answer 2>/dev/null < /dev/tty; then
    if [ -z "$answer" ]; then
      echo "y"
    else
      printf '%s\n' "$answer" | tr '[:upper:]' '[:lower:]'
    fi
  else
    # No tty - accept the default (echo it to stderr to finish the prompt line)
    echo "y" >&2
    echo "y"
  fi
}

OS="$(detect_os)"
ARCH="$(detect_arch)"
VERSION="$(resolve_version)"
BIN_DIR="$(resolve_install_dir)"
TAG="v${VERSION}"

EXT=""
if [ "$OS" = "windows" ]; then
  EXT=".exe"
fi

ASSET="preloop-${OS}-${ARCH}${EXT}"
URL="https://github.com/${PRELOOP_REPO}/releases/download/${TAG}/${ASSET}"
CHECKSUMS_URL="https://github.com/${PRELOOP_REPO}/releases/download/${TAG}/SHA256SUMS"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

mkdir -p "$BIN_DIR"
DOWNLOAD_PATH="${TMP_DIR}/preloop${EXT}"
curl -fsSL "$URL" -o "$DOWNLOAD_PATH"

# SHA256SUMS verification is defense in depth. Do not block installation if
# GitHub's checksum asset is temporarily unavailable or malformed.
if curl -fsSL "$CHECKSUMS_URL" -o "${TMP_DIR}/SHA256SUMS"; then
  expected_hash="$(awk -v asset="$ASSET" '$2 == asset || $2 == "*" asset { print $1; exit }' "${TMP_DIR}/SHA256SUMS")"
  if [ -z "$expected_hash" ]; then
    echo "Warning: could not find a SHA256 checksum for ${ASSET}; continuing without verification." >&2
  elif command -v sha256sum >/dev/null 2>&1; then
    actual_hash="$(sha256sum "$DOWNLOAD_PATH" | awk '{print $1}')"
    if [ "$actual_hash" != "$expected_hash" ]; then
      echo "Warning: SHA256 verification failed for ${ASSET}; continuing with the downloaded file." >&2
    else
      echo "  SHA256 verified"
    fi
  elif command -v shasum >/dev/null 2>&1; then
    actual_hash="$(shasum -a 256 "$DOWNLOAD_PATH" | awk '{print $1}')"
    if [ "$actual_hash" != "$expected_hash" ]; then
      echo "Warning: SHA256 verification failed for ${ASSET}; continuing with the downloaded file." >&2
    else
      echo "  SHA256 verified"
    fi
  else
    echo "Warning: no SHA256 tool is available; continuing without verification." >&2
  fi
else
  echo "Warning: could not download SHA256SUMS; continuing without verification." >&2
fi

chmod +x "$DOWNLOAD_PATH"
mv "$DOWNLOAD_PATH" "${BIN_DIR}/preloop${EXT}"

PRELOOP_BIN="${BIN_DIR}/preloop${EXT}"

# Seed the install identifier the CLI reports during its daily version check
# (distinguishes installed clients in adoption telemetry; a random UUID, no
# user data). The CLI generates one on first run if this seeding is skipped.
CLIENT_ID_FILE="${HOME}/.preloop/client_id"
if [ ! -f "$CLIENT_ID_FILE" ]; then
  mkdir -p "${HOME}/.preloop" && chmod 700 "${HOME}/.preloop" || true
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr '[:upper:]' '[:lower:]' > "$CLIENT_ID_FILE" 2>/dev/null || true
  elif [ -r /proc/sys/kernel/random/uuid ]; then
    cat /proc/sys/kernel/random/uuid > "$CLIENT_ID_FILE" 2>/dev/null || true
  fi
  [ -f "$CLIENT_ID_FILE" ] && chmod 600 "$CLIENT_ID_FILE" || true
fi

echo "Installed preloop ${VERSION} to ${PRELOOP_BIN}"
case ":$PATH:" in
  *":${BIN_DIR}:"*) ;;
  *)
    echo "Note: ${BIN_DIR} is not on your PATH."
    ;;
esac

# Step 1: discover local AI agents BEFORE asking the user to authenticate, so
# they can see what Preloop found and decide whether to log in / sign up to
# onboard them. The discover command itself is read-only and never mutates
# local files or the user's account, so it is safe to run unconditionally.
discovered_agents=0
echo ""
echo "Looking for AI agents on this machine..."
if discover_output="$("$PRELOOP_BIN" agents discover --no-onboard-prompt 2>&1)"; then
  printf '%s\n' "$discover_output"
  if printf '%s' "$discover_output" | grep -q '^Found '; then
    discovered_agents=1
  fi
else
  printf '%s\n' "$discover_output" >&2
  echo "(Continuing - agent discovery is optional.)"
fi

# Step 2: choose the Preloop instance to connect to. The CLI talks to
# Preloop Cloud (https://preloop.ai) by default, but works identically
# against a self-hosted open-source instance. Honors a pre-set PRELOOP_URL,
# otherwise offers the choice interactively. The chosen URL is exported so
# `preloop login` / `preloop signup` / `preloop agents discover` below all
# target the same instance (and persist it to ~/.preloop/config.yaml).
PRELOOP_CLOUD_URL="https://preloop.ai"
target_url="${PRELOOP_URL:-$PRELOOP_CLOUD_URL}"
echo ""
if [ -n "${PRELOOP_URL:-}" ]; then
  echo "Preloop instance: ${target_url} (from PRELOOP_URL)"
elif preloop_confirm_set; then
  echo "Preloop instance: ${target_url} (Preloop Cloud)"
else
  echo "The CLI connects your agents to a Preloop control plane:"
  echo "  - Preloop Cloud: ${PRELOOP_CLOUD_URL} (default, requires an account)"
  echo "  - Self-hosted:   your own open-source instance, e.g. http://localhost:8000"
  echo "    (install it with: curl -fsSL https://preloop.ai/install/oss | sh)"
  printf 'Preloop instance URL [%s]: ' "$target_url"
  if read -r custom_url < /dev/tty 2>/dev/null; then
    custom_url="$(printf '%s' "$custom_url" | tr -d '[:space:]')"
    if [ -n "$custom_url" ]; then
      target_url="$custom_url"
    fi
  fi
  echo "Preloop instance: ${target_url}"
fi
PRELOOP_URL="$target_url"
export PRELOOP_URL

# existing_login_identity echoes the identity lines of an already-verified
# login and returns 0; it returns 1 when there is no usable session.
#
# `preloop auth status` exits 0 in every case, including its degraded verdicts
# ("Authenticated (stored login could not be refreshed)", "Authenticated
# (token may be invalid or server unreachable)") which mean the CLI never
# confirmed the session with the server. Only the exact "Authenticated" line
# counts as signed in; everything else must fall through to a real login.
existing_login_identity() {
  status_output="$("$1" auth status 2>/dev/null || true)"
  printf '%s\n' "$status_output" | grep -q '^Authenticated$' || return 1
  printf '%s\n' "$status_output" | grep -E '^  (User|Email|Org):' || true
  return 0
}

# Step 3: offer to authenticate — unless this machine already has a verified
# session, in which case say who it belongs to and go straight to onboarding.
# If we found local agents we tell the user explicitly that logging in lets us
# onboard them.
echo ""
if identity_lines="$(existing_login_identity "$PRELOOP_BIN")"; then
  echo "Already signed in to ${target_url}:"
  [ -n "$identity_lines" ] && printf '%s\n' "$identity_lines"
  echo "Run 'preloop login --force' to switch accounts."
  if [ "$discovered_agents" = "1" ]; then
    echo ""
    onboard_ans="$(prompt_default_yes "Onboard discovered agents now? [Y/n]")"
    case "$onboard_ans" in
      y|yes)
        "$PRELOOP_BIN" agents discover < /dev/tty || true
        ;;
    esac
  fi
  exit 0
fi

if [ "$discovered_agents" = "1" ]; then
  prompt="Sign in (or sign up) to ${target_url} now to onboard the agents above? [Y/s/n]"
else
  prompt="Sign in (or sign up) to ${target_url} now? [Y/s/n]"
fi

if preloop_confirm_set; then
  printf '%s y (PRELOOP_CONFIRM)\n' "$prompt"
  auth_choice="y"
else
  printf '%s ' "$prompt"
  if read -r auth_choice < /dev/tty 2>/dev/null; then
    auth_choice="$(printf '%s' "$auth_choice" | tr '[:upper:]' '[:lower:]')"
    [ -z "$auth_choice" ] && auth_choice="y"
  else
    auth_choice="n"
  fi
fi

auth_command=""
case "$auth_choice" in
  y|yes|l|login) auth_command="login" ;;
  s|signup|register|r) auth_command="signup" ;;
  *) auth_command="" ;;
esac

if [ -n "$auth_command" ]; then
  if "$PRELOOP_BIN" "$auth_command" < /dev/tty; then
    if [ "$discovered_agents" = "1" ]; then
      echo ""
      onboard_ans="$(prompt_default_yes "Onboard discovered agents now? [Y/n]")"
      case "$onboard_ans" in
        y|yes)
          # `preloop agents discover` re-prints the listing and then walks
          # through each candidate with a (Y/n) onboarding prompt. When
          # PRELOOP_CONFIRM is set in the environment the inner prompts are
          # auto-approved, matching the unattended behavior of this script.
          "$PRELOOP_BIN" agents discover < /dev/tty || true
          ;;
      esac
    fi
  else
    echo "Authentication encountered an error or was aborted."
  fi
else
  if [ "$target_url" = "$PRELOOP_CLOUD_URL" ]; then
    echo "Skipped authentication. Run 'preloop login' or 'preloop signup' when you're ready."
  else
    echo "Skipped authentication. When you're ready, run:"
    echo "  preloop login --url ${target_url}"
  fi
fi
