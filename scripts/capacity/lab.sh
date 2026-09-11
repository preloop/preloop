#!/usr/bin/env bash
# Manage only the disposable local capacity Compose project.
set -euo pipefail
export PRELOOP_DISABLE_TELEMETRY=true
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo_root"
if [[ -n "${DOCKER_HOST:-}" ]]; then
  echo 'Use a local Unix-socket Docker context without DOCKER_HOST.' >&2
  exit 1
fi
docker context inspect | python3 -c 'import json,sys; endpoint=json.load(sys.stdin)[0]["Endpoints"]["docker"]["Host"]; assert endpoint.startswith("unix://"), "Local Docker required"'
export CAPACITY_REVISION=${CAPACITY_REVISION:-$(git rev-parse HEAD)}
compose=(docker compose -f scripts/capacity/compose.yaml)
action=${1:-help}
if [[ $# -gt 0 ]]; then shift; fi
case "$action" in
  up)
    if [[ -z "${CAPACITY_IMAGE:-}" ]]; then "${compose[@]}" build; fi
    "${compose[@]}" up -d --wait api gateway fake
    ;;
  run)
    run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
    artifact_dir="scripts/capacity/artifacts/$run_id"
    mkdir -p "$artifact_dir"
    "${compose[@]}" config > "$artifact_dir/compose.yaml"
    "${compose[@]}" images --format json > "$artifact_dir/images.json"
    python3 scripts/capacity/collect.py --seconds 86400 --output "$artifact_dir/resources.jsonl" &
    collector_pid=$!
    finish() {
      kill "$collector_pid" 2>/dev/null || true
      wait "$collector_pid" 2>/dev/null || true
      "${compose[@]}" logs --no-color > "$artifact_dir/services.log" 2>&1 || true
      echo "Artifacts: $artifact_dir"
    }
    trap finish EXIT
    "${compose[@]}" run --rm load python -m scripts.capacity.run "$@" --output "/artifacts/$run_id/run"
    ;;
  down)
    "${compose[@]}" down --volumes --remove-orphans
    ;;
  *)
    echo 'Usage: scripts/capacity/lab.sh up | run [driver arguments] | down'
    ;;
esac
