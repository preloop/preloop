#!/usr/bin/env bash
# Shared helpers for the e2e rig. Sourced by the entrypoints and bash modules.
#
# Contract: entrypoints export RIG_HOST, RIG_URL, RIG_RELEASE, RIG_CLI_VERSION,
# RIG_RUN_DIR, RIG_PYTHON before any module runs. Modules exit 0 (pass),
# 3 (skip, recorded), anything else (fail).

set -euo pipefail

RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RIG_DIR

# STANDING RULE: the rig always sets PRELOOP_DISABLE_TELEMETRY so test runs
# never pollute funnel/adoption data. Exported here for every module (local
# CLI/tool invocations) and injected into every remote command by rig_ssh.
export PRELOOP_DISABLE_TELEMETRY=true

rig_log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

rig_note() {
  # Append a human-readable note that ends up in run-summary.json for the
  # current step (the orchestrator collects $RIG_STEP_NOTES_FILE).
  rig_log "NOTE: $*"
  if [ -n "${RIG_STEP_NOTES_FILE:-}" ]; then
    printf '%s\n' "$*" >> "$RIG_STEP_NOTES_FILE"
  fi
}

rig_die() {
  rig_log "FATAL: $*" >&2
  exit 1
}

rig_skip() {
  rig_note "$*"
  exit 3
}

# All remote work goes through one ssh entrypoint so BatchMode (never prompt
# for credentials) and timeouts are uniform. Remote command is a single string
# run by the login shell.
rig_ssh() {
  # Every remote command carries the telemetry opt-out so CLI/installer runs
  # on the target host never emit adoption telemetry.
  # Note: rig_ssh expects a single command string, not multiple args.
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$RIG_HOST" \
    "export PRELOOP_DISABLE_TELEMETRY=true; $*"
}

rig_scp_from() {
  # rig_scp_from <remote-path> <local-path>
  scp -o BatchMode=yes -q "$RIG_HOST:$1" "$2"
}

# The union of config paths (relative to $HOME) that `preloop agents
# onboard/offboard` may touch, taken from cli/internal/cmd/agents.go
# (agentSpecs) plus ~/.claude.json and ~/.claude/mcp.json which hold Claude
# Code's model/MCP state. Snapshots read these; nothing in the rig ever
# writes to them.
RIG_AGENT_CONFIG_PATHS=(
  ".claude.json"
  ".claude/settings.json"
  ".claude/settings.local.json"
  ".claude/mcp-servers.json"
  ".claude/mcp.json"
  ".claude/claude_desktop_config.json"
  ".config/claude/claude_desktop_config.json"
  ".cursor/mcp.json"
  ".codeium/windsurf/mcp_config.json"
  ".vscode/mcp.json"
  ".gemini/settings.json"
  ".gemini/config/mcp_config.json"
  ".gemini/antigravity/mcp_config.json"
  ".config/opencode/config.json"
  ".codex/config.toml"
  ".codex/config.json"
  ".openclaw/openclaw.json"
  ".openclaw/openclaw.json5"
  ".openclaw-dev/openclaw.json"
  ".openclaw-dev/openclaw.json5"
  ".hermes/config.yaml"
  ".hermes/config.yml"
  ".config/devin/config.json"
  ".devin/config.json"
)
export RIG_AGENT_CONFIG_PATHS_STR="${RIG_AGENT_CONFIG_PATHS[*]:-}"

# rig_snapshot <label>
# Copies every existing agent config file from the VM into
# $RIG_RUN_DIR/snapshots/<label>/home/ and writes a sha256 manifest plus the
# CLI's own view of the world (`preloop agents list`). Read-only on the VM.
rig_snapshot() {
  local label="$1"
  local dest="$RIG_RUN_DIR/snapshots/$label"
  mkdir -p "$dest/home"

  local paths_str="${RIG_AGENT_CONFIG_PATHS[*]}"
  # Build the file list remotely (only files that exist), then stream one tar.
  # shellcheck disable=SC2029
  rig_ssh "cd \$HOME && for p in $paths_str; do [ -f \"\$p\" ] && printf '%s\n' \"\$p\"; done | tar czf - -T - 2>/dev/null || true" \
    > "$dest/home.tar.gz"
  if [ -s "$dest/home.tar.gz" ]; then
    tar xzf "$dest/home.tar.gz" -C "$dest/home"
  fi
  rm -f "$dest/home.tar.gz"

  # Manifest: stable order, sha256 + size per file.
  (
    cd "$dest/home"
    find . -type f | sort | while read -r f; do
      shasum -a 256 "$f"
    done
  ) > "$dest/manifest.sha256" || true

  # Context (best-effort): what the CLI thinks is enrolled, agent versions.
  rig_ssh 'preloop agents list 2>&1 || true' > "$dest/preloop-agents-list.txt" 2>/dev/null || true
  rig_ssh 'for b in preloop claude opencode codex; do
             if command -v "$b" >/dev/null 2>&1; then
               printf "%s: %s\n" "$b" "$(command -v "$b")"
             else
               printf "%s: NOT INSTALLED\n" "$b"
             fi
           done' > "$dest/binaries.txt" 2>/dev/null || true
  rig_ssh 'ls -la ~/.preloop/agents/backups/ 2>/dev/null; echo; ls ~/.preloop/agents/state/ 2>/dev/null' \
    > "$dest/preloop-local-state.txt" 2>/dev/null || true

  rig_log "snapshot '$label' -> $dest ($(wc -l < "$dest/manifest.sha256" | tr -d ' ') files)"
}

# Compose invocation for the instance dir on the VM. Only includes overlay
# files that actually exist, mirroring how install-oss.sh started the stack.
RIG_INSTANCE_DIR="/root/.preloop-oss"
export RIG_INSTANCE_DIR

rig_compose_args_remote() {
  # Prints a shell fragment (for remote eval) that sets $COMPOSE_ARGS.
  cat <<'EOF'
COMPOSE_ARGS="-f docker-compose.yaml"
[ -f docker-compose.auth.yaml ] && COMPOSE_ARGS="$COMPOSE_ARGS -f docker-compose.auth.yaml"
[ -f docker-compose.tls.yaml ] && COMPOSE_ARGS="$COMPOSE_ARGS -f docker-compose.tls.yaml"
[ -f docker-compose.rig-telemetry.yaml ] && COMPOSE_ARGS="$COMPOSE_ARGS -f docker-compose.rig-telemetry.yaml"
EOF
}
