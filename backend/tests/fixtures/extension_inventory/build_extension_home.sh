#!/bin/bash
# Build a realistic scratch HOME for the extension inventory fixture: the
# public gstack skill pack and a few official marketplace plugins, plus
# synthetic hooks and MCP servers (example.com hosts, no credentials).
#
# Usage: build_extension_home.sh <home> <downloads>
# <downloads> holds clones of github.com/garrytan/gstack and
# github.com/anthropics/claude-plugins-official. Then regenerate with
#   HOME=<home> PRELOOP_DISABLE_TELEMETRY=true preloop agents discover --inventory \
#     | jq '{generated_by: "backend/tests/fixtures/extension_inventory/build_extension_home.sh",
#            apps: [.apps[] | select((.extensions|length)>0) | {app_id, extensions}]}'
set -euo pipefail
H=${1:?home}; DL=${2:?downloads}
rm -rf "$H"; mkdir -p "$H/.claude/skills" "$H/.claude/plugins/cache/claude-plugins-official" "$H/.claude/plugins/marketplaces" "$H/.codex/skills"
for d in "$DL"/gstack/*/; do
  n=$(basename "$d"); [ -f "$d/SKILL.md" ] || continue
  mkdir -p "$H/.claude/skills/$n"
  (cd "$d" && find . -type f \( -name '*.md' -o -path './scripts/*' -o -path './bin/*' \) -not -path '*/node_modules/*' -size -200k | cpio -pdm --quiet "$H/.claude/skills/$n")
done
mkdir -p "$H/.claude/skills/gstack"; cp "$DL/gstack/SKILL.md" "$H/.claude/skills/gstack/"
for n in careful freeze review ship; do mkdir -p "$H/.codex/skills/$n"; cp "$DL/gstack/$n/SKILL.md" "$H/.codex/skills/$n/"; done
M="$H/.claude/plugins/marketplaces/claude-plugins-official"; mkdir -p "$M/.claude-plugin"
cp "$DL/claude-plugins-official/.claude-plugin/marketplace.json" "$M/.claude-plugin/"
entries=""; enabled=""
for p in commit-commands code-review security-guidance hookify feature-dev pyright-lsp; do
  v=$(jq -r '.version // "unknown"' "$DL/claude-plugins-official/plugins/$p/.claude-plugin/plugin.json")
  dst="$H/.claude/plugins/cache/claude-plugins-official/$p/$v"; mkdir -p "$dst"
  (cd "$DL/claude-plugins-official/plugins/$p" && find . -type f -not -path './tests/*' | cpio -pdm --quiet "$dst")
  entries="$entries${entries:+,}\"$p@claude-plugins-official\":[{\"scope\":\"user\",\"installPath\":\"$dst\",\"version\":\"$v\",\"installedAt\":\"2026-11-01T10:00:00.000Z\",\"lastUpdated\":\"2026-11-20T10:00:00.000Z\",\"gitCommitSha\":\"0000000000000000000000000000000000000000\"}]"
  enabled="$enabled${enabled:+,}\"$p@claude-plugins-official\":true"
done
echo "{\"version\":2,\"plugins\":{$entries}}" | jq . > "$H/.claude/plugins/installed_plugins.json"
jq -n --arg loc "$M" '{"claude-plugins-official":{source:{source:"github",repo:"anthropics/claude-plugins-official"},installLocation:$loc,lastUpdated:"2026-11-20T10:00:00.000Z"}}' > "$H/.claude/plugins/known_marketplaces.json"
mkdir -p "$H/.claude/hooks-bin"
jq -n --argjson en "{$enabled}" '{enabledPlugins:$en, hooks:{
 PreToolUse:[{matcher:"*",hooks:[{type:"command",command:"preloop agents permission-hook"}]}],
 PostToolUse:[{matcher:"*",hooks:[{type:"command",command:"preloop usage hook"}]}],
 SessionStart:[{matcher:"",hooks:[{type:"command",command:"preloop agents session-hook"}]},{matcher:"^(startup|resume|clear|compact|fork)$",hooks:[{type:"command",command:"bash -c \"exec ~/.claude/skills/gstack/bin/gstack-session-start\""}]}],
 PreCompact:[{hooks:[{type:"command",command:"~/.claude/hooks-bin/backup-transcript.sh"}]}],
 Stop:[{hooks:[{type:"command",command:"~/.claude/skills/gstack/bin/timeline-stop-hook"}]},{matcher:"",hooks:[{type:"command",command:"preloop agents stop-hook"}]}],
 UserPromptSubmit:[{matcher:"",hooks:[{type:"command",command:"preloop usage hook"}]}]}}' > "$H/.claude/settings.json"
printf '#!/bin/sh\ncp "$1" "$1.bak"\n' > "$H/.claude/hooks-bin/backup-transcript.sh"
jq -n '{mcpServers:{preloop:{type:"http",url:"https://preloop.example.com/mcp/v1"},playwright:{command:"npx",args:["@playwright/mcp@latest"]}},projects:{}}' > "$H/.claude.json"
printf '[mcp_servers.docs]\nurl = "https://docs.example.com/mcp"\n\n[mcp_servers.filesystem]\ncommand = "npx"\nargs = ["-y", "@modelcontextprotocol/server-filesystem"]\n' > "$H/.codex/config.toml"
