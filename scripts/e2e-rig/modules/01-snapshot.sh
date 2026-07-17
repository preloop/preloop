#!/usr/bin/env bash
# Module 01 — preflight + before-image snapshot.
#
# Reads (never writes) every known agent config file on the VM into
# snapshots/before/. This is the before-image for the offboarding assertion.
# Also records which agents/CLI binaries are installed.

set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

rig_log "preflight: $RIG_HOST"
rig_ssh 'uname -a; docker --version' || rig_die "VM preflight failed (ssh/docker)"

rig_snapshot "before"

# Record what is running right now for the run report.
rig_ssh 'docker ps --format "{{.Names}} {{.Image}} {{.Status}}"' \
  > "$RIG_RUN_DIR/snapshots/before/docker-ps.txt" || true
rig_ssh "cat $RIG_INSTANCE_DIR/.env 2>/dev/null | sed 's/\(SECRET_KEY\|POSTGRES_PASSWORD\|SMTP_PASSWORD\)=.*/\1=<redacted>/'" \
  > "$RIG_RUN_DIR/snapshots/before/instance-env-redacted.txt" || true

count=$(wc -l < "$RIG_RUN_DIR/snapshots/before/manifest.sha256" | tr -d ' ')
[ "$count" -gt 0 ] || rig_note "no agent config files found on the VM"
rig_note "before-image captured: $count agent config files"
