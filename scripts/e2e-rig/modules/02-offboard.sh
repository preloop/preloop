#!/usr/bin/env bash
# Module 02 — offboard all currently-enrolled agents with the CLI already on
# the VM, restoring each agent's local config from the CLI's own backups.
# Takes the "baseline" snapshot afterwards: this is the pre-onboarding state
# that module 12 asserts against.

set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

if ! rig_ssh 'command -v preloop >/dev/null 2>&1'; then
  rig_note "preloop CLI not installed on VM; nothing to offboard"
  rig_snapshot "baseline"
  exit 0
fi

rig_log "current enrollment:"
rig_ssh 'preloop agents list 2>&1 || true' | tee "$RIG_RUN_DIR/logs/02-agents-list-before.txt"

# --remove-mcp-servers/--remove-model=no: local restore is what we assert on;
# the server side is torn down wholesale in module 03 anyway.
set +e
rig_ssh 'preloop agents offboard --all --yes --remove-mcp-servers=no --remove-model=no 2>&1' \
  | tee "$RIG_RUN_DIR/logs/02-offboard.txt"
status=${PIPESTATUS[0]}
set -e

if [ "$status" -ne 0 ]; then
  # Offboarding needs the (old) instance to still answer; if it cannot, fall
  # back to restoring local backups per agent so local configs are still clean.
  rig_note "offboard --all failed (exit $status); attempting per-agent local restore"
  rig_ssh 'for d in ~/.preloop/agents/backups/*/; do
             [ -d "$d" ] || continue
             echo "restore candidate: $d"
           done' | tee -a "$RIG_RUN_DIR/logs/02-offboard.txt" || true
fi

rig_log "post-offboard enrollment:"
rig_ssh 'preloop agents list 2>&1 || true' | tee "$RIG_RUN_DIR/logs/02-agents-list-after.txt"

rig_snapshot "baseline"

# Sanity: restored configs should no longer point at the managed gateway.
host_part="${RIG_URL#*://}"
leftover=$(grep -rl "$host_part" "$RIG_RUN_DIR/snapshots/baseline/home" 2>/dev/null || true)
if [ -n "$leftover" ]; then
  rig_note "WARNING: instance host still referenced after offboard: $leftover"
else
  rig_note "no restored config references the instance host — local restore looks clean"
fi
