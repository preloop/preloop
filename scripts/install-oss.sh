#!/usr/bin/env sh

set -eu

PRELOOP_REPO="${PRELOOP_REPO:-preloop/preloop}"
PRELOOP_DEFAULT_VERSION="${PRELOOP_DEFAULT_VERSION:-}"
PRELOOP_VERSION="${PRELOOP_VERSION:-$PRELOOP_DEFAULT_VERSION}"
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.preloop-oss}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
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

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  fi
}

require_command curl
require_command docker

VERSION="$(resolve_version)"
TAG="v${VERSION}"
COMPOSE_URL="https://github.com/${PRELOOP_REPO}/releases/download/${TAG}/docker-compose.release.yaml"

mkdir -p "$INSTALL_DIR"
curl -fsSL "$COMPOSE_URL" -o "${INSTALL_DIR}/docker-compose.yaml"

if [ ! -f "${INSTALL_DIR}/.env" ]; then
  cat > "${INSTALL_DIR}/.env" <<EOF
PRELOOP_VERSION=${VERSION}
SECRET_KEY=$(generate_secret)
POSTGRES_PASSWORD=$(generate_secret)
EOF
fi

compose_status=0
(
  cd "$INSTALL_DIR"
  docker compose up -d
) || compose_status=$?

echo "Preloop OSS ${VERSION} is starting in ${INSTALL_DIR}"
echo "API: http://localhost:8000"
echo "Console: http://localhost:3000"
echo ""
echo "Next steps:"
echo "  1. Open http://localhost:3000 and create the first user"
echo "  2. Install the CLI (if you haven't):"
echo "       curl -fsSL https://preloop.ai/install/cli | sh"
echo "  3. Connect the CLI to THIS instance (not preloop.ai):"
echo "       preloop login --url http://localhost:8000"
echo "  4. Onboard your local agents:"
echo "       preloop agents discover"
echo ""
echo "To stop it later:"
echo "  cd ${INSTALL_DIR} && docker compose down"

if [ "$compose_status" -ne 0 ]; then
  echo ""
  echo "Docker Compose failed. Inspect logs with:"
  echo "  cd ${INSTALL_DIR} && docker compose logs"
  exit "$compose_status"
fi
