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

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    arm64|aarch64) echo "arm64" ;;
    *)
      echo "Unsupported architecture: $(uname -m)" >&2
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

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

mkdir -p "$BIN_DIR"
curl -fsSL "$URL" -o "${TMP_DIR}/preloop${EXT}"
chmod +x "${TMP_DIR}/preloop${EXT}"
mv "${TMP_DIR}/preloop${EXT}" "${BIN_DIR}/preloop${EXT}"

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

# Step 3: offer to authenticate. If we found local agents we tell the user
# explicitly that logging in lets us onboard them.
echo ""
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
