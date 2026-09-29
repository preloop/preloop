# OpenClaw Integration

After this page you have a local OpenClaw installation whose MCP tool calls go through the Preloop firewall, whose model traffic goes through the Preloop gateway, whose **native** tool calls (shell, file writes) wait for your approval on phone/watch/web, and which you can message live from the console.

!!! info "Example Policies"
    Ready-to-use policy files: [OpenClaw Policy Examples](../examples/policies/openclaw/README.md).

---

## Why govern OpenClaw

OpenClaw runs locally and can execute shell commands, modify files, browse the web, and talk to external services. Preloop puts a decision point in front of that:

| Without Preloop | With Preloop |
|-----------------|--------------|
| Any shell command runs immediately | Dangerous commands wait for approval |
| Files modified silently | Sensitive paths trigger review |
| No audit trail | Every governed call logged with the matched rule and approver |
| All-or-nothing access | Ordered allow / deny / approval rules per tool |
| Model spend invisible | Gateway attribution, budgets, session analytics |

---

## Onboard with the CLI

```bash
curl -fsSL https://preloop.ai/install/cli | sh   # if you don't have the CLI yet

preloop agents discover                  # detect OpenClaw (read-only, then offers onboarding)
preloop agents onboard openclaw --dry-run  # preview every change first
preloop agents onboard openclaw
preloop agents install-plugin openclaw   # Agent Control + native approvals plugin
preloop agents validate openclaw
```

(`preloop agents enroll` is an alias for `onboard`. Don't have OpenClaw installed yet? `preloop agents install-runtime openclaw` installs it and onboards in one step.)

Onboarding, against the default config at `~/.openclaw/openclaw.json`:

1. **Backs up** the original config next to it, so `preloop agents restore openclaw` can undo everything local.
2. **Imports** the MCP servers configured in OpenClaw into your Preloop account where they can be represented, then rewrites the local MCP config so OpenClaw talks only to the managed `preloop` entry — governed tool access through the MCP firewall.
3. **Imports the configured model** and rewrites OpenClaw to send model traffic through Preloop's **OpenAI-compatible gateway** with a managed credential. This works for Gemini models too (e.g. `google/gemini-3.1-pro-preview`): Preloop preserves the upstream provider identity and routes through the gateway alias. If your OpenClaw build cannot express the provider/base-URL rewrite, MCP governance still completes and model settings stay manual — the onboarding summary tells you.
4. **Creates the control-plane records**: a managed agent (`agent_kind: openclaw`), a durable runtime credential bound to it, and a runtime-session history — so this installation has one durable identity for audit, spend attribution, and operator controls.
5. **Writes the Agent Control config** for the runtime plugin (next section).

Two config patterns are handled on import: provider/model details under `models.providers`, and provider auth under `auth.profiles` (common for Google/Gemini). With `auth.profiles`, the model identity imports and the rewrite happens, but you may need to add the upstream provider secret in Preloop separately.

After enrollment, [subject-scoped governance](../concepts/subject-scoped-governance.md) applies: account defaults → managed-agent scope → API-key scope, so this specific OpenClaw runtime can be limited to fewer tools and models than the rest of the account.

### Rollback

```bash
preloop agents restore openclaw    # restore the local config backup
preloop agents offboard openclaw   # restore config + remove the managed enrollment
                                   # (--remove-model / --remove-mcp-servers to also clean up imports)
```

---

## The Agent Control plugin

The runtime plugin is `@preloop-ai/openclaw-plugin` (v0.1.1 on npm, Apache-2.0, vendored in the Preloop repo under `runtime-plugins/openclaw-preloop`). It owns the live side of the integration: the long-lived WebSocket to `/api/v1/agents/control/ws`, reconnect/backoff, presence/heartbeat, capability advertisement, operator-message delivery — and **native tool approvals**.

Install it through OpenClaw's plugin manager:

```bash
preloop agents install-plugin openclaw
# equivalent to:
openclaw plugins install @preloop-ai/openclaw-plugin
```

Then restart OpenClaw. When the plugin connects and advertises capabilities, Preloop marks Agent Control verified and Talk controls appear for the agent in the console and mobile apps.

Notes:

- The plugin installer runs inside OpenClaw's Node runtime — Node **>= 20** is required. If OpenClaw reports `requires Node` or `Unsupported engine`, upgrade the Node executable `openclaw` uses and rerun `preloop agents install-plugin openclaw`.
- If native plugin installation is impossible, the CLI can fall back to Preloop's managed Agent Control sidecar so control readiness can still be verified.
- The plugin's config is written by the CLI under `plugins.entries.preloop-plugin.config` (the plugin's runtime id is `preloop-plugin`) with `control_ws_url`, `bearer_token`, and `runtime_principal_id`. Do not hand-author the `bearer_token`, and do not write Agent Control metadata as a top-level `preloop` object — OpenClaw builds that validate config schemas reject unknown root keys.
- Validation reports `control_config_written`, `control_plugin_installed`, `control_plugin_verified`, and `control_channel_configured` separately, so MCP/gateway onboarding succeeds even while the plugin is not yet loaded.

### Native tool approvals (`before_tool_call`)

MCP governance only sees tool calls that go through the MCP server. OpenClaw's *native* tools — shell exec, file writes — never do. The plugin closes that gap: its `before_tool_call` hook intercepts every native tool call and asks Preloop for a decision at `POST /api/v1/agents/permission-check`, which runs your approval workflows and blocks for up to ~300 seconds while you decide on web, mobile, or watch. `deny` blocks the tool with the reason; anything else lets it run.

- **Fail-closed by default.** If Preloop is unreachable, the tool call is blocked. Set `tool_approval_fail_open: true` in the plugin config only if you accept ungoverned execution during outages. `tool_approval_enabled: false` disables the gate entirely.
- **Local pre-filter.** The plugin honors OpenClaw's own `~/.openclaw/exec-approvals.json` first: locally denied commands are denied outright, locally allowed ones run untouched — only calls that *would have prompted you* round-trip to Preloop.
- Per-subject control: the `native_tool_approvals: enforce | off` governance toggle, and a pinned approval workflow per agent (Console → the agent's governance settings).

Approval requests from this path show tool source `agent` and land in the same audit trail as MCP calls.

---

## Manual MCP configuration

If you would rather wire only the MCP firewall by hand (no gateway, no Agent Control):

```json
{
  "mcp": {
    "servers": {
      "preloop": {
        "transport": "http",
        "url": "https://YOUR_PRELOOP_URL/mcp/v1",
        "headers": {
          "Authorization": "Bearer YOUR_API_KEY"
        }
      }
    }
  }
}
```

Create the API key in the console under **Settings → API Keys**. The CLI-managed path above is strictly better: durable per-agent identity, gateway routing, backups, and validation.

---

## Policies for OpenClaw tools

Manage policies as YAML and apply them from the CLI:

```bash
preloop policy validate shell-approval.yaml
preloop policy apply shell-approval.yaml
preloop policy apply -r policies/          # apply a directory
preloop policy diff shell-approval.yaml    # preview against the live account
preloop policy list
```

The canonical shape (see [Policy-as-Code](../concepts/policy-as-code.md) for the full schema):

```yaml
approval_workflows:
  - name: "shell-approval"
    timeout_seconds: 300
    approvals_required: 1

tools:
  - name: "bash"
    source: mcp
    approval_workflow: "shell-approval"
    conditions:
      - expression: "args.command.contains('sudo ') || args.command.contains('rm -rf')"
        condition_type: "cel"
        action: "require_approval"
        description: "Privilege escalation and destructive deletes"
```

Conditions evaluate against `args` (the tool-call arguments) — that is the only variable bound. Compound expressions and functions like `contains()` / `startsWith()` / `endsWith()` need `condition_type: "cel"`; the default simple evaluator handles single comparisons (`==`, `!=`, `>`, `<`, `>=`, `<=`). Rules are ordered; first match wins; `deny` beats waiting.

The [example policies](../examples/policies/openclaw/README.md) cover shell safety, file protection, and browser safety, ready to `preloop policy apply`.

---

## Troubleshooting

**MCP server not responding**

```bash
curl -fsS https://YOUR_PRELOOP_URL/api/v1/health
openclaw mcp list
```

Check the API key is valid, has no stray whitespace, and was not revoked.

**Managed enrollment succeeded, but model traffic still bypasses Preloop**

- Verify the imported AI model in Preloop has gateway routing enabled.
- Confirm the rewritten OpenClaw provider/base-URL points at Preloop's OpenAI-compatible gateway.
- If the original config used `auth.profiles`, add the upstream provider secret in Preloop.
- MCP governance is active even while the model rewrite needs manual follow-up.

**Plugin installed but the agent never shows online**

- Restart OpenClaw after installing the plugin.
- `preloop agents validate openclaw` — check the `control_*` fields.
- Check OpenClaw's Node version (>= 20).

**Agent appears in Preloop but no session activity**

- Start a fresh OpenClaw session and trigger at least one MCP or model action.
- Check **Agents** and **Runtime Sessions** in the console.
- If model calls were expected, verify the runtime uses the managed gateway path.

**Policy not triggering**

- `preloop policy diff <file>` to confirm what is live.
- Conditions with functions or `||`/`&&` need `condition_type: "cel"`.
- Rules evaluate in order — an earlier `allow` wins.

More: [Troubleshooting](../troubleshooting.md).

---

## Related

- [OpenClaw Policy Examples](../examples/policies/openclaw/README.md)
- [Agent Control Runtime Adapters](agent-control-runtime-adapters.md) — the wire protocol and adapter contract
- [Conditional Approval (CEL)](../approvals/cel-expressions.md)
- [Team-Based Approvals](../approvals/teams.md)
- [Mobile Apps](../clients/mobile-apps.md)
