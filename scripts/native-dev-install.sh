#!/usr/bin/env bash
# Idempotent app-level install for a native (no Docker) host.
# System packages come from Dockerfile.dev or an equivalent OS install.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

python_bin="${PYTHON:-}"
if [ -z "$python_bin" ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    python_bin=python3.12
  else
    python_bin=python3
  fi
fi

if [ ! -d .venv ]; then
  "$python_bin" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"

npm --prefix frontend install

if [ ! -f .env ]; then
  cat > .env <<'EOF'
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/preloop
NATS_URL=nats://localhost:4222
SECRET_KEY=dev-secret-not-for-production
PRELOOP_SERVICE_ROLE=all
PRELOOP_DISABLE_TELEMETRY=true
DEBUG=true
HOST=0.0.0.0
PORT=8000
EOF
  echo "Wrote $root/.env with native-dev defaults."
fi
