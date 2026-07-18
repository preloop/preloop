# Govern your OpenClaw agent — approvals on your phone, spend you can see

Your agent decides to run `kubectl delete deployment api`. You are not at the
keyboard. Right now it just runs.

`@preloop-ai/openclaw-plugin` connects OpenClaw to
[Preloop](https://github.com/preloop/preloop), the open-source AI agent control
plane. With it, that command pauses, the request lands on your phone, watch,
Slack, Mattermost, email, or the web console, and you tap **Approve** or
**Deny**. The agent continues or gets blocked with your reason. Everything it
did is recorded.

**Two things to be clear about, because they install differently:**

- **The plugin** gates OpenClaw's native tool calls — shell commands, file
  writes, everything the model can reach without asking — and holds open a
  durable control channel, so you can message or interrupt an already-running
  session from anywhere, typed or dictated.
- **Onboarding OpenClaw to Preloop** (one extra CLI command, below) routes its
  MCP and model traffic through Preloop, which is what produces per-agent cost
  attribution, budgets, allowed-model lists, and session cost optimization. The
  plugin does not do this on its own.

Install the plugin and you get governance. Onboard the agent and you also get
the bill, itemized — across every agent you run, not just OpenClaw.

![The Preloop console showing live agents — OpenClaw, Hermes, Claude Code and Codex CLI — connected through the Preloop gateway, each with its own request count, spend, and Talk button](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/agent_bubble.png)

It is Apache-2.0, and it works against either the open-source
[Preloop](https://github.com/preloop/preloop) control plane you host yourself,
or the hosted [Preloop Cloud](https://preloop.ai).

**Watch it work** — onboarding, tool governance, approvals, and cutting session
cost, recorded end to end against a real stack:

[![Preloop video series — see your agents, govern them, cut their cost](https://img.youtube.com/vi/Y_geb2Or8zM/maxresdefault.jpg)](https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt)

## Install

Node **>= 20** is required — OpenClaw runs the plugin installer inside its own
Node runtime.

```bash
openclaw plugins install @preloop-ai/openclaw-plugin
# or from ClawHub:
# openclaw plugins install clawhub:@preloop-ai/openclaw-plugin
```

Restart OpenClaw afterwards. If OpenClaw reports `requires Node` or
`Unsupported engine`, upgrade the Node executable `openclaw` uses and reinstall.

You also need a Preloop control plane to approve against — either
[Preloop Cloud](https://preloop.ai) (nothing to run) or the open-source stack on
your own machine:

```bash
curl -fsSL https://preloop.ai/install/oss | sh
```

### Or let the Preloop CLI do all of it

If you have (or want) the [Preloop CLI](https://docs.preloop.ai), it discovers
your OpenClaw install, backs up the config, installs this plugin, and writes the
credentials for you:

```bash
curl -fsSL https://preloop.ai/install/cli | sh
preloop signup                          # or: preloop login --url http://localhost:8000
preloop agents onboard openclaw
preloop agents install-plugin openclaw
preloop agents validate openclaw
```

This is the path that also routes OpenClaw's MCP tool calls through the Preloop
MCP firewall and its model traffic through the Preloop gateway — the budgets and
per-agent cost attribution described above. The plugin alone covers native tool
approvals and the control channel.

Undo anything with `preloop agents restore openclaw` or
`preloop agents offboard openclaw`.

## What you get, and from which piece

| | Without Preloop | Plugin installed | Agent also onboarded |
|---|---|---|---|
| Risky native tool calls | `rm -rf`, `terraform apply`, `git push --force` run the moment the model decides to | Your rules decide: run, block, or hold for a human | same |
| Approving | Means sitting in front of the terminal | Phone, watch, Slack, Mattermost, email, or console | same |
| Reaching a running agent | Unreachable once it starts | Message or interrupt it from console or mobile | same |
| Record of what happened | Terminal scrollback | Every governed call logged with matched rule, approver, outcome | plus model calls and MCP calls |
| MCP tool calls | Ungoverned | Ungoverned — the plugin does not see them | Governed by the MCP firewall |
| Model spend | Invisible | Still invisible | Attributed per agent, session, and model; budgets enforced |
| Wasted tokens | Unmeasured | Unmeasured | Evidence-grounded waste findings per session |

Concretely, for the approval path: the `before_tool_call` hook intercepts the
call and asks Preloop for a decision. Preloop matches your policy, sees this
needs a human, and pushes a notification with the full command to your phone.
The tool call blocks for up to ~300 seconds while you decide. You deny it;
OpenClaw gets the block plus your reason, and the agent carries on with
something else.

## Configuration

The plugin reads its own OpenClaw plugin entry, at
`plugins.entries.preloop-plugin.config` in `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "preloop-plugin": {
        "config": {
          "enabled": true,
          "protocol": "preloop.agent_control.v1",
          "runtime": "openclaw",
          "control_ws_url": "wss://app.preloop.ai/api/v1/agents/control/ws",
          "bearer_token": "agt_...",
          "runtime_principal_id": "openclaw-...",
          "runtime_principal_name": "OpenClaw"
        }
      }
    }
  }
}
```

Do not hand-author `bearer_token` — let the CLI or the marketplace installer
mint it. Do not write Agent Control metadata as a top-level `preloop` object
either; OpenClaw builds that validate config schemas reject unknown root keys.

| Key | Default | What it does |
|---|---|---|
| `tool_approval_enabled` | `true` | Set to `false` to turn the native tool-call gate off entirely |
| `tool_approval_fail_open` | `false` | Fail-closed by default: if Preloop is unreachable, the tool call is **blocked**. Set `true` only if you accept ungoverned execution during an outage |
| `permission_check_url` | derived from `control_ws_url` | Override the approval endpoint |

### How a decision is made

1. **OpenClaw's own policy runs first.** The plugin reads
   `~/.openclaw/exec-approvals.json`. Commands your local policy denies are
   denied outright; ones it allows run untouched. Only calls that *would have
   prompted you* round-trip to Preloop, so the hook stays cheap.
   Allowlist-miss cases are escalated rather than resolved locally — the plugin
   does not reimplement OpenClaw's command analyzer.
2. **Preloop evaluates your policy.** Allow, deny, require approval, or require
   a written justification, expressed as YAML + CEL.
3. **A human decides, if the policy says so.** Notification to mobile, watch,
   Slack, Mattermost, email, or webhook; the tool call blocks up to ~300s.
4. **The result is recorded** in the same audit trail as MCP tool calls, tagged
   with tool source `agent`.

## Manual Test Without Preloop CLI

The plugin does not need the Preloop CLI at runtime. To check an install by
hand:

```bash
openclaw plugins install @preloop-ai/openclaw-plugin
preloop-openclaw-plugin verify --config ~/.openclaw/openclaw.json
preloop-openclaw-plugin run --config ~/.openclaw/openclaw.json
```

`verify` checks the config shape and that the plugin loads. `run` opens the
Agent Control WebSocket and advertises capabilities without OpenClaw attached.
In Preloop the agent should show as online, and Talk controls should appear in
the console and mobile apps. To test message delivery end to end, run the
plugin inside OpenClaw itself (not via `run`, which has no session attached),
then pick the OpenClaw agent in the console, click Talk, and send a short
message.

## What the plugin actually does

Scoped honestly, so you know what you are installing:

- Maintains a WebSocket to Preloop's Agent Control endpoint, with exponential
  reconnect backoff (2s → 30s) and a 30s heartbeat, so the channel survives
  laptop sleep and network changes.
- Advertises capabilities: new and existing sessions, text, voice transcripts,
  interrupt, tool approval.
- Delivers operator messages and voice transcripts into the running OpenClaw
  session, and relays interrupts.
- Gates every native tool call through `before_tool_call`, fail-closed.

What it does **not** do: it is not a content filter or a prompt-injection
defense. Preloop's protection against prompt injection is partial — policy and
approvals mean an injected instruction still has to get past your rules and, for
anything risky, past you. That is a meaningful barrier, not a guarantee. It also
does not itself route MCP or model traffic, and so it does not by itself produce
cost attribution, budgets, or optimization findings; those come from Preloop
onboarding.

## What onboarding unlocks

[Preloop](https://github.com/preloop/preloop) is the open-source AI agent
control plane (Apache-2.0, self-hostable, with
[Preloop Cloud](https://preloop.ai) as a hosted option). Once an agent's traffic
runs through it, alongside approvals you get:

- **MCP firewall** — allow / deny / require-approval / require-justification on
  every MCP tool call, as YAML + CEL policy.
- **AI model gateway** — OpenAI- and Anthropic-compatible, with per-agent
  budgets, allowed-model lists, and cost attribution. Provider keys stay with
  Preloop instead of inside agent containers.
- **Cost analytics and budgets** — spend explained by model, agent, session, API
  key, and user, with soft and hard budget ceilings and budget-health alerts.
- **Session cost optimization** — evidence-grounded waste findings per session,
  one-click apply, and consent-gated replay verification of the savings. This
  ships in the open-source core, using your own model keys.
- **Runtime session observability** — one timeline per session covering tool
  calls, model calls, policy decisions, approvals, and spend.
- **Audit trails** — durable records with the matched policy, approver, inputs,
  timestamps, and outcome.

![The Preloop cost view: estimated spend and token totals for the period, per-agent cost breakdown, and budget health against soft and hard ceilings](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/cost_page.png)

![The Preloop audit timeline: a live, filterable stream of model requests, runtime sessions, token counts, cost per call, and outcomes](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/audit_page.png)

The point of the combination: one control plane over every agent you run, not a
separate dashboard per runtime. Preloop works with any MCP-compatible agent —
OpenClaw, Claude Code, Codex CLI, Cursor, Gemini CLI, Hermes, OpenCode, and
others. The same OpenClaw, still running at full speed, but now something you
can see while it works, stop before it does damage, talk to from anywhere, and
account for afterwards.

**Editions:** *Preloop* is the open-source edition. *Preloop Cloud* is the
hosted service. *Preloop Enterprise* is the commercial self-hosted edition.

## Learn more

- Docs: [docs.preloop.ai](https://docs.preloop.ai) —
  [OpenClaw integration guide](https://docs.preloop.ai/guide/integrations/openclaw/)
- Source: [github.com/preloop/preloop](https://github.com/preloop/preloop) —
  this plugin lives in
  [`runtime-plugins/openclaw-preloop`](https://github.com/preloop/preloop/tree/main/runtime-plugins/openclaw-preloop)
- Video series:
  [Preloop on YouTube](https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt)
- Issues: [github.com/preloop/preloop/issues](https://github.com/preloop/preloop/issues)

Apache-2.0. Copyright (c) 2026 Spacecode AI Inc.
