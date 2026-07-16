#!/usr/bin/env bash
# Teardown-only entrypoint: archive the instance's .env/config, bring the
# compose stack down (volumes removed), prune old preloop images.
# Let's Encrypt material under /root/.preloop-oss/certbot is preserved unless
# --purge-certs is given (re-issuing too often trips LE rate limits).
#
# Usage: scripts/e2e-rig/teardown-oss.sh --host root@<vm> [--run-dir DIR] [--purge-certs]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$HERE/lib/common.sh"

RIG_HOST=""
RUN_DIR=""
RIG_PURGE_CERTS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host) RIG_HOST="$2"; shift 2 ;;
    --run-dir) RUN_DIR="$2"; shift 2 ;;
    --purge-certs) RIG_PURGE_CERTS=1; shift ;;
    *) rig_die "unknown argument: $1" ;;
  esac
done
[ -n "$RIG_HOST" ] || rig_die "--host is required"

if [ -z "$RUN_DIR" ]; then
  RUN_DIR="$HERE/artifacts/teardown-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$RUN_DIR/instance-archive" "$RUN_DIR/logs" "$RUN_DIR/state"
RIG_RUN_DIR="$(cd "$RUN_DIR" && pwd)"

export RIG_HOST RIG_RUN_DIR RIG_PURGE_CERTS
export RIG_STEP_NOTES_FILE="$RIG_RUN_DIR/state/notes-teardown.txt"
: > "$RIG_STEP_NOTES_FILE"

bash "$HERE/modules/03-teardown.sh" 2>&1 | tee "$RIG_RUN_DIR/logs/teardown.log"
