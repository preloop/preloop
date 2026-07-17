#!/usr/bin/env bash
# Module 05 — install the specified Preloop OSS release at the specified URL,
# using the documented release install path (the release's own install-oss.sh,
# which fetches docker-compose.release.yaml and sets up nginx+certbot TLS).
#
# Registration is left OPEN (PRELOOP_SKIP_ADMIN=1) so module 07 can perform
# the first-user browser signup. SMTP prompts are skipped.
#
# Known installer bug worked around here: when a Let's Encrypt cert already
# exists, issue_certificate() returns before swapping tls/active.conf to the
# HTTPS server block, leaving the proxy HTTP-only. We detect and fix that.

set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

INSTALLER_URL="https://github.com/preloop/preloop/releases/download/v${RIG_RELEASE}/install-oss.sh"
LOG="$RIG_RUN_DIR/logs/05-install.txt"

rig_log "installing Preloop OSS ${RIG_RELEASE} at ${RIG_URL} (installer: $INSTALLER_URL)"

# TODO(remove-after-#65): installer workaround — run with bash instead of sh.
# PR #65 (approved, unmerged) fixes has_tty() under dash; once a release ships
# with it, drop the bash indirection and use the documented `sh` invocation.
#
# ssh without -t => no tty on the VM => the installer never prompts.
# Run with bash, not sh: on Debian sh is dash, where has_tty()'s
# `: < /dev/tty` redirect failure is FATAL for the special builtin when no
# controlling tty exists, killing unattended TLS installs that leave
# PRELOOP_TLS_EMAIL unset (installer bug — report upstream). bash degrades
# gracefully and has_tty simply returns false.
set +e
rig_ssh "curl -fsSL '$INSTALLER_URL' -o /tmp/preloop-install-oss.sh &&
         PRELOOP_DISABLE_TELEMETRY=true \
         PRELOOP_VERSION='$RIG_RELEASE' \
         PRELOOP_URL='$RIG_URL' \
         PRELOOP_SKIP_SMTP=1 \
         PRELOOP_SKIP_ADMIN=1 \
         bash /tmp/preloop-install-oss.sh" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e
[ "$status" -eq 0 ] || rig_die "installer exited $status (see $LOG)"

# Server-side phone-home off: the test instance must never show up in
# adoption data. The var goes into the instance .env, and — because the
# pinned release's compose file may not map it into the api container — a
# small rig overlay injects it into the api service (the only role that
# phones home; see backend/preloop/services/instance_service.py).
# rig_compose_args_remote picks the overlay up for all later compose calls.
rig_ssh "
  set -e
  cd $RIG_INSTANCE_DIR
  grep -q '^PRELOOP_DISABLE_TELEMETRY=' .env 2>/dev/null \
    || printf 'PRELOOP_DISABLE_TELEMETRY=true\n' >> .env
  printf '%s\n' \
    '# Written by the e2e rig: test instances must not send adoption telemetry.' \
    'services:' \
    '  api:' \
    '    environment:' \
    '      PRELOOP_DISABLE_TELEMETRY: \${PRELOOP_DISABLE_TELEMETRY:-true}' \
    > docker-compose.rig-telemetry.yaml
  $(rig_compose_args_remote)
  docker compose \$COMPOSE_ARGS up -d api
" | tee -a "$LOG"
rig_note "telemetry opt-out applied: PRELOOP_DISABLE_TELEMETRY=true in instance .env + api overlay"

# TODO(remove-after-#65): installer workaround — HTTPS fixup for the
# pre-existing-cert path described above. PR #65 (approved, unmerged) makes
# the installer activate HTTPS itself on cert reuse; drop this whole block
# once a release ships with it.
host_part="${RIG_URL#https://}"
host_part="${host_part%%/*}"
rig_ssh "
  set -e
  cd $RIG_INSTANCE_DIR
  if [ -f certbot/conf/live/$host_part/fullchain.pem ] && [ -f tls/https.conf ]; then
    if ! grep -q 'listen 443' tls/active.conf; then
      cp tls/https.conf tls/active.conf
      $(rig_compose_args_remote)
      docker compose \$COMPOSE_ARGS up -d proxy certbot
      docker compose \$COMPOSE_ARGS exec -T proxy nginx -s reload 2>/dev/null || true
      echo HTTPS_FIXUP_APPLIED
    fi
  fi
" | tee -a "$LOG" | grep -q HTTPS_FIXUP_APPLIED \
  && rig_note "applied HTTPS fixup: installer leaves tls/active.conf HTTP-only when the cert already exists (installer bug — report upstream)" \
  || true

rig_ssh 'docker ps --format "{{.Names}} {{.Image}} {{.Status}}"' | tee -a "$LOG"
rig_note "release $RIG_RELEASE installed in $RIG_INSTANCE_DIR"
