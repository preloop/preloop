#!/usr/bin/env bash
# Start a disposable full stack only for explicitly selected application checks.
set -euo pipefail
export PRELOOP_DISABLE_TELEMETRY=true
export PRELOOP_SERVICE_ROLE=all
export HOST=0.0.0.0
export PORT=8000
if [[ ${PRELOOP_DISPOSABLE_ENVIRONMENT:-} != true ]]; then
  echo 'Application checks require an operator-approved disposable environment.' >&2
  exit 78
fi
if [[ $# -eq 0 ]]; then
  echo 'Provide the repository application test command.' >&2
  exit 64
fi
mkdir -p /workspace/evidence
backend_pid=''
frontend_pid=''
cleanup() {
  [[ -z "$backend_pid" ]] || kill -- "-$backend_pid" 2>/dev/null || true
  [[ -z "$frontend_pid" ]] || kill -- "-$frontend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
.venv/bin/python scripts/init_db.py --force
setsid .venv/bin/python -m preloop.server > /workspace/evidence/backend.log 2>&1 &
backend_pid=$!
setsid npm --prefix frontend run dev -- --host 0.0.0.0 > /workspace/evidence/frontend.log 2>&1 &
frontend_pid=$!
python3 - <<'PY'
import time
import urllib.error
import urllib.request
end = time.monotonic() + 90
for url in ('http://127.0.0.1:8000/api/v1/health', 'http://127.0.0.1:5173'):
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, TimeoutError):
            pass
        if time.monotonic() >= end:
            raise SystemExit('Disposable application health check failed')
        time.sleep(0.5)
PY
"$@"
