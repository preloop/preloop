#!/usr/bin/env bash
# Module 04 — ensure the agent matrix is installed on the VM.
#
# claude + opencode gate the run; codex is installed only when possible
# non-interactively WITHOUT provisioning credentials (npm install). Existing
# agent auth state is never touched — installs are binaries only.

set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

LOG="$RIG_RUN_DIR/logs/04-install-agents.txt"
: > "$LOG"

ensure() {
  local name="$1" install_cmd="$2"
  if rig_ssh "command -v $name >/dev/null 2>&1"; then
    rig_log "$name: already installed ($(rig_ssh "command -v $name"))"
    return 0
  fi
  rig_log "$name: installing"
  if rig_ssh "$install_cmd" >> "$LOG" 2>&1 && rig_ssh "command -v $name >/dev/null 2>&1"; then
    rig_note "$name installed"
  else
    return 1
  fi
}

ensure claude 'npm install -g @anthropic-ai/claude-code' \
  || rig_die "claude missing and install failed (gates the run)"
ensure opencode 'curl -fsSL https://opencode.ai/install | bash' \
  || rig_die "opencode missing and install failed (gates the run)"

# Codex: install is credential-free; running it is not. We install the binary
# and (only if absent) drop a minimal config marker so `preloop agents
# discover` can see it. We never run codex itself or create auth.
if ensure codex 'npm install -g @openai/codex'; then
  rig_ssh 'if [ ! -e ~/.codex/config.toml ] && [ ! -e ~/.codex/config.json ]; then
             mkdir -p ~/.codex
             printf "# created by preloop e2e-rig so agent discovery can detect codex\n" > ~/.codex/config.toml
             echo "created ~/.codex/config.toml marker"
           fi'
  rig_note "codex: installed (no credentials provisioned — sessions that need auth will be skipped)"
else
  rig_note "codex: install failed or unavailable non-interactively — skipped (recorded)"
fi

rig_ssh 'for b in claude opencode codex; do
           if command -v "$b" >/dev/null 2>&1; then
             printf "%s: %s\n" "$b" "$(command -v $b)"
           else
             printf "%s: NOT INSTALLED\n" "$b"
           fi
         done' | tee -a "$LOG"

# Fresh installs above may have created config files (e.g. the codex marker)
# that the module-02 baseline predates. Refresh the baseline — nothing is
# onboarded at this point, so it is still the pre-onboarding state that
# module 14 must restore to.
rig_snapshot "baseline"
