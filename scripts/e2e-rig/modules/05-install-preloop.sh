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

# ssh without -t => no tty on the VM => the installer never prompts.
# Run with bash, not sh: on Debian sh is dash, where has_tty()'s
# `: < /dev/tty` redirect failure is FATAL for the special builtin when no
# controlling tty exists, killing unattended TLS installs that leave
# PRELOOP_TLS_EMAIL unset (installer bug — report upstream). bash degrades
# gracefully and has_tty simply returns false.
set +e
rig_ssh "curl -fsSL '$INSTALLER_URL' -o /tmp/preloop-install-oss.sh &&
         PRELOOP_VERSION='$RIG_RELEASE' \
         PRELOOP_URL='$RIG_URL' \
         PRELOOP_SKIP_SMTP=1 \
         PRELOOP_SKIP_ADMIN=1 \
         bash /tmp/preloop-install-oss.sh" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e
[ "$status" -eq 0 ] || rig_die "installer exited $status (see $LOG)"

# HTTPS fixup for the pre-existing-cert path described above.
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
