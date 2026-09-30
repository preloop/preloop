# Claude Code Reference

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

How to onboard Claude Code (Anthropic's agentic coding CLI) into Preloop's Safety Layer with the `preloop` CLI.

---

## Overview

Preloop governs Claude Code on two planes:

- **Tool calls**: a managed `preloop` MCP server entry routes governed tools through the **Preloop MCP Firewall**, where access rules (allow / deny / require_approval) and approval workflows apply.
- **Model traffic**: onboarding can rewrite Claude Code's environment so model calls go through the **Preloop Gateway** using a managed model alias, giving you cost analytics and budget controls.

Optionally, Preloop can also install a **native tool-approval hook** so Claude Code's own tool calls (shell commands, file edits) that would normally prompt you in the terminal are routed to Preloop mobile/watch/web approvals instead.

---

## Prerequisites

- Claude Code installed, see [Anthropic's installation docs](https://docs.anthropic.com/en/docs/claude-code) (e.g. `npm install -g @anthropic-ai/claude-code`).
- The Preloop CLI:

```bash
curl -fsSL https://preloop.ai/install/cli | sh
```

The installer discovers local agents, walks you through login/signup, and offers to onboard everything it found. See the [CLI quickstart](../quickstart-cli.md) for the full flow.

<!-- TODO screenshot: `claude-cli-version.png` -->

---

## Onboard Claude Code

### Step 1: Discover

```bash
preloop agents discover
```

This scans standard configuration paths without mutating anything. For Claude Code it reads `~/.claude/settings.json` (primary) or `~/.claude/mcp-servers.json` (legacy fallback) and lists any MCP servers already configured there. Fresh or lightly-used installs are detected too: `~/.claude.json` or a `claude` binary on `PATH` count as install markers, and onboarding bootstraps the canonical `~/.claude/settings.json`.

### Step 2: Onboard

```bash
preloop agents onboard "claude code"
```

Or run `preloop agents discover` and accept the interactive onboarding prompt. Onboarding:

1. Creates (or locates) the managed agent identity in your Preloop account and issues a **durable credential** for it.
2. **Backs up** your existing Claude Code config next to the original so you can roll back at any time.
3. Adds a managed `preloop` MCP server entry to `~/.claude/settings.json`.
4. Rewrites supported model configuration so Claude Code's Anthropic traffic routes through the Preloop Gateway (sets `env.ANTHROPIC_BASE_URL` to your Preloop gateway's `/anthropic` endpoint, `env.ANTHROPIC_API_KEY` to the managed credential, and lets stock Opus/Sonnet/Haiku selectors follow Claude Code defaults).
5. Runs a live validation prompt through the agent (disable with `--skip-live-validate`).

Useful flags (see `preloop agents onboard --help`):

| Flag | Effect |
|------|--------|
| `--dry-run` | Preview the planned account and config changes without writing anything |
| `--yes` / `-y` | Skip confirmation prompts |
| `--all` | Onboard every discovered agent |
| `--approvals` | Also install the native tool-permission hook (see below) |
| `--pin-model-families` | Persist explicit stock family pins for gateways with Claude family autoregistration disabled |
| `--skip-live-validate` | Skip the post-onboarding live validation prompt |
| `--tags key=value` | Add key-value tags to the enrolled agent |

<!-- TODO screenshot: `claude-mcp-list-connected.png` -->

Stock families follow Claude Code defaults, so new Anthropic releases need no manual refresh: after a Claude Code update, the gateway registers first-used identifiers against the agent's subscription credential. Fable and custom models retain explicit mappings. For a gateway with Claude family autoregistration disabled, pass `--pin-model-families` on onboard or refresh. The choice persists across later runs; `preloop agents refresh "claude code" --pin-model-families=false` clears it. With explicit pins enabled, refresh verifies family upgrades against the live provider list.

### What gets written

After onboarding, `~/.claude/settings.json` contains a managed entry like:

```json
{
  "servers": {
    "preloop": {
      "url": "https://preloop.ai/mcp/v1",
      "transport": "http-streaming",
      "headers": {
        "Authorization": "Bearer <managed credential>"
      }
    }
  }
}
```

!!! note "Configuration file location"
    Preloop treats `~/.claude/settings.json` as Claude Code's primary configuration file. `~/.claude/mcp-servers.json` is only read as a legacy fallback when `settings.json` is absent. When the `claude` binary is on `PATH`, the CLI registers the managed server through `claude mcp add --scope user` instead of editing files directly.

### Step 3: Verify

```bash
preloop agents status "claude code"     # local + remote enrollment state
preloop agents validate "claude code"   # config validation
preloop agents validate "claude code" --live  # plus a live prompt through the agent
preloop agents list                     # all managed agents in your account
```

You can also open **`https://preloop.ai/console/agents`**: Claude Code appears as a card with its onboarding state (`Fully onboarded`, `MCP proxy only`, `Model gateway only`, or `Incomplete`).

!!! note "Pro/Max subscriptions (OAuth) work too"
    If your Claude Code runs on a Pro/Max subscription instead of an API key, onboarding stores the OAuth credential and the gateway proxies that traffic **byte-faithfully**: Anthropic requires the exact Claude Code request shape, so Preloop forwards it untouched while still applying budgets, governance tool-stripping, and usage recording. Message-level context optimizations do not apply to this traffic, and the credential is bound to this agent's model: other agents cannot use it. If the post-onboarding live check is throttled or refused upstream, the agent shows an **unverified** badge until `preloop agents validate "claude code" --live` passes. Details: [Model Gateway → Subscription OAuth Passthrough](../concepts/model-gateway.md#subscription-oauth-passthrough).

---

## Native tool approvals (PreToolUse hook)

Claude Code has powerful native tools (shell, file edits) that never pass through an MCP server. To govern those too, onboard with:

```bash
preloop agents onboard "claude code" --approvals
```

(Interactive onboarding also offers this as a `Route Claude Code's native tool calls ... through Preloop approvals?` prompt.)

This installs a `PreToolUse` hook entry in `~/.claude/settings.json` that runs `preloop agents permission-hook --source claude_code` before each tool call, plus a per-agent credential under `~/.preloop/agents/`. Because Claude Code's `PreToolUse` fires on *every* tool call, the hook first evaluates your own Claude Code permission settings and only escalates calls that **would have prompted you**, those become Preloop approval requests you can answer from the [mobile apps](mobile-apps.md), watch, or web console. Calls your own config already allows proceed untouched.

<!-- TODO screenshot: `claude-cli-approval-flow.png` -->

---

## Using governed tools

Once onboarded, tools exposed through the managed `preloop` MCP server are subject to your account's access rules and approval workflows. When Claude Code invokes a tool that requires approval, the call blocks until you approve or deny it (dashboard, email, or mobile), then the result is returned to the agent as usual.

Multi-step workflows behave the same way: each governed tool call in the sequence creates its own approval request as your rules dictate.

<!-- TODO screenshot: `claude-multi-tool-workflow.png` -->

The always-available `request_approval` tool lets the agent explicitly ask for human sign-off with custom context, see [Built-in Tools](../tools/builtin.md).

---

## Rollback and offboarding

Onboarding always backs up the original config, so you can undo everything:

```bash
preloop agents restore "claude code"    # restore the most recent local backup
preloop agents offboard "claude code"   # restore config and remove managed enrollment
```

`offboard` also removes the approvals hook and its credential if they were installed, and can optionally clean up the managed model and MCP servers in your account (`--remove-model`, `--remove-mcp-servers`).

---

## Related

- [CLI quickstart](../quickstart-cli.md): the 60-second install-and-onboard flow
- [Safety Layer & Access Rules](../concepts/safety-layer.md): how rules are evaluated
- [AI Model Gateway](../concepts/model-gateway.md): what gateway routing gives you
- [Mobile Apps](mobile-apps.md): approve on the go
