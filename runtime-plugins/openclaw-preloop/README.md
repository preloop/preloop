# Govern your OpenClaw agent: approvals on your phone, spend you can see

Your agent decides to run `kubectl delete deployment api`. You are not at the
keyboard. Right now it just runs.

`@preloop-ai/openclaw-plugin` connects OpenClaw to
[Preloop](https://github.com/preloop/preloop), the open-source AI agent control
plane. With the plugin installed, that command pauses, the request lands on your
phone, watch, Slack, Mattermost, email, or the web console, and you tap
**Approve** or **Deny**. The agent continues or gets blocked with your reason.
Everything it did is recorded.

## What you get

- **Dangerous commands wait for you instead of just running.** Every native tool
  call is checked before it runs: your local OpenClaw policy first, then your
  central Preloop policy. Allow, deny, or hold for a human, per tool and per
  argument.
- **Approve from wherever you actually are.** Push to phone or watch, Slack,
  Mattermost, email, an outbound webhook, or the Preloop console. The tool call
  stays blocked while you decide, for up to 24 hours by default, so "I will look
  at it after lunch" is a valid answer.
- **Nothing runs ungoverned when Preloop is down.** The gate fails closed by
  default. An unreachable control plane, a 5xx, a bad token, or a malformed
  reply all block the call rather than waving it through.
- **Talk to an agent that is already running.** A durable control channel stays
  open, so you can send a message, dictate one, or interrupt the current turn
  from the console or the mobile apps, long after you walked away from the
  terminal.
- **A record of what the agent did, and who let it.** Every governed call is
  logged with the matched rule, the approver, the arguments, and the outcome.
- **One screen for every agent you run, with its bill.** Per-agent spend, budget
  ceilings that stop the spend, and a session timeline. This part needs the
  agent onboarded to Preloop as well as the plugin installed. See
  [What onboarding unlocks](#what-onboarding-unlocks).

![The Preloop console overview: pending approvals waiting for a decision, budget health against soft and hard ceilings, and the list of active agents with per-agent spend and a Talk button on each](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/dashboard.png)

**Two pieces, because they install differently:**

- **The plugin** (this package) gates OpenClaw's native tool calls, the shell
  commands and file writes the model can reach without asking, and holds open
  the control channel for messages and interrupts.
- **Onboarding OpenClaw to Preloop** (one extra CLI command, below) routes its
  MCP and model traffic through Preloop, which is what produces per-agent cost
  attribution, budgets, allowed-model lists, and session cost optimization. The
  plugin does not do this on its own.

Install the plugin and you get governance. Onboard the agent and you also get
the bill, itemized, across every agent you run rather than just OpenClaw.

It is Apache-2.0, and it works against either the open-source
[Preloop](https://github.com/preloop/preloop) control plane you host yourself,
or the hosted [Preloop Cloud](https://preloop.ai).

**Watch it work.** Onboarding, tool governance, approvals, and cutting session
cost, recorded end to end against a real stack:

[![Preloop video series: see your agents, govern them, cut their cost](https://img.youtube.com/vi/Y_geb2Or8zM/maxresdefault.jpg)](https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt)

## 60 seconds to install

Node **>= 20** is required, because OpenClaw runs the plugin installer inside
its own Node runtime.

```bash
openclaw plugins install clawhub:@preloop-ai/openclaw-plugin
```

That is the ClawHub listing. The plain npm package works too:

```bash
openclaw plugins install @preloop-ai/openclaw-plugin
```

Restart OpenClaw afterwards. If OpenClaw reports `requires Node` or
`Unsupported engine`, upgrade the Node executable `openclaw` uses and reinstall.

You also need a Preloop control plane to approve against, either
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
preloop signup                          # or: preloop login --url http://localhost:3000
preloop agents onboard openclaw
preloop agents install-plugin openclaw
preloop agents validate openclaw
```

This is the path that also routes OpenClaw's MCP tool calls through the Preloop
MCP firewall and its model traffic through the Preloop gateway, which is where
the budgets and per-agent cost attribution come from. The plugin alone covers
native tool approvals and the control channel.

Undo anything with `preloop agents restore openclaw` or
`preloop agents offboard openclaw`.

Do not hand-author the runtime bearer token. Let the CLI or the marketplace
installer mint it.

## What happens on the first dangerous tool call

The model picks a tool. Before OpenClaw runs it, the plugin's
`before_tool_call` hook stops it and works through four steps.

1. **Your local OpenClaw policy runs first.** The plugin reads
   `~/.openclaw/exec-approvals.json`. If your own policy denies the command, it
   is denied outright and never leaves the machine. A local allow is still sent
   to Preloop so central rules can narrow it, and an allowlist miss is escalated
   rather than resolved locally, because the plugin does not reimplement
   OpenClaw's command analyzer.
2. **Preloop evaluates your central policy.** Rules match on the tool name and
   its arguments, written either as simple conditions or as CEL expressions,
   and the whole policy can be exported and imported as YAML. A rule can allow
   the call, deny it, or require a human. Most calls resolve here without
   bothering anyone. If no rule matches, a local allow stands.
3. **If a human is required, the request goes out and the tool waits.** A
   notification with the full command lands on your phone, watch, Slack,
   Mattermost, email, an outbound webhook, or the console. The tool call is
   still blocked while you think. The default wait budget is 24 hours.
4. **The answer comes back and is recorded.** Approve and the tool runs. Deny
   and OpenClaw receives a block plus the reason you gave, which the agent sees
   as the tool result and usually works around. Either way the decision is
   written to the audit trail, tagged with tool source `agent`, next to your
   MCP tool calls.

**When things go wrong, the call blocks.** Only a valid `allow` lets a tool run.
A returned `deny`, including an approval that expired (`timed_out: true`),
blocks even if you turned fail-open on. Transport errors, timeouts and HTTP 5xx
block by default and are the only failures that `tool_approval_fail_open: true`
can waive. HTTP 4xx (including 401 and 403), malformed replies and invalid
configuration always block, fail-open or not.

![The Preloop tools page with rules on an MCP tool: deny above one threshold, require approval in the middle band, allow below it, each rule written as a CEL expression over the tool arguments](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/rules_configured.png)

## Configuration reference

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

Do not write Agent Control metadata as a top-level `preloop` object. OpenClaw
builds that validate config schemas reject unknown root keys. `enabled: false`
is the supported pause switch: the package stays installed, but the plugin
registers no hooks and opens no control channel. The pause switch applies
when OpenClaw loads the plugin through `register()`. The standalone
`preloop-openclaw-plugin run` command still starts the Agent Control channel;
that is an explicit CLI start, not host registration.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | Set to `false` to keep the package installed but register nothing: no Agent Control channel and no approval hook. `tool_approval_enabled` only turns the gate off; uninstall removes the plugin |
| `tool_approval_enabled` | `true` | Set to `false` to turn the native tool-call gate off entirely |
| `tool_approval_fail_open` | `false` | Fail-closed by default: if Preloop is unreachable, the tool call is **blocked**. Set `true` only if you accept ungoverned execution during an outage |
| `tool_approval_timeout_seconds` | `86400` | Workflow wait budget, an integer from 30 to 86400 seconds; HTTP adds 15 seconds of headroom |
| `permission_check_url` | derived from `control_ws_url` | Override the approval endpoint |

### Wait budgets

The default budget covers approval workflows up to 24 hours, including a
workflow selected by a central rule. It is a maximum wait, not a new workflow
timeout: a five-minute workflow still expires after five minutes. Set
`tool_approval_timeout_seconds` lower only if it covers every workflow this
agent can select, because a shorter transport deadline can interrupt a pending
approval and then follows the failure behaviour above. Missing config uses the
default; invalid values block and are rejected by `verify` and by the manifest.

Bundled standalone and Helm nginx configurations allow 86460 seconds only on
`/api/v1/agents/permission-check`. Other ingress, load-balancer and OpenClaw
host limits must also permit the chosen wait.

Turning the plugin gate off skips central enforcement entirely. Preloop's
server-side approvals-off setting only disables human escalation and does not
override an explicit central require-approval rule.

### Where the plugin metadata lives

`openclaw.plugin.json` carries only the fields in OpenClaw's published
`PluginManifest` type (`id`, `name`, `description`, `version`, `configSchema`).
As of OpenClaw 2026.7.2-beta.7 the ClawHub validator rejects any other
top-level key. Packaging and runtime metadata (the `before_tool_call` hook, the
`tool_approval` capability, the permission strings, the config path, and the
`preloop-openclaw-plugin verify` command) live under the `openclaw` object in
`package.json` instead. Nothing about plugin behaviour changed: the plugin
reads its config through the OpenClaw plugin entry and registers its hook in
code, not from manifest declarations.

## Manual Test Without Preloop CLI

The plugin does not need the Preloop CLI at runtime. To check an install by
hand:

```bash
openclaw plugins install @preloop-ai/openclaw-plugin
preloop-openclaw-plugin verify --config ~/.openclaw/openclaw.json
preloop-openclaw-plugin run --config ~/.openclaw/openclaw.json
```

`verify` checks the config shape and that the plugin loads. `run` opens the
Agent Control WebSocket and advertises capabilities without OpenClaw attached,
including when `enabled` is `false`, because `run` is an explicit CLI start
rather than the host `register()` path.
In Preloop the agent should show as online, and Talk controls should appear in
the console and mobile apps. To test message delivery end to end, run the
plugin inside OpenClaw itself (not via `run`, which has no session attached),
then pick the OpenClaw agent in the console, click Talk, and send a short
message.

## What the plugin actually does

Scoped honestly, so you know what you are installing:

- Maintains a WebSocket to Preloop's Agent Control endpoint, with exponential
  reconnect backoff (2s to 30s) and a 30s heartbeat, so the channel survives
  laptop sleep and network changes. A server eviction (close code 4000, meaning
  a newer connection superseded this one) stops the retry loop instead of
  fighting it.
- Advertises capabilities: new and existing sessions, text, voice transcripts,
  interrupt, tool approval.
- Delivers operator messages and voice transcripts into the running OpenClaw
  session, and relays interrupts.
- Gates every native tool call through `before_tool_call`, fail-closed.

What it does **not** do: it is not a content filter or a prompt-injection
defense. Preloop's protection against prompt injection is partial. Policy and
approvals mean an injected instruction still has to get past your rules and, for
anything risky, past you. That is a meaningful barrier, not a guarantee. The
plugin only governs calls delivered to its `before_tool_call` hook, so it cannot
promise coverage for tools that bypass that host hook, and it does not see
OpenClaw's MCP tool calls at all: those are governed by the MCP firewall, which
requires onboarding. It also does not itself route model traffic, and so it does
not by itself produce cost attribution, budgets, or optimization findings.

## What onboarding unlocks

[Preloop](https://github.com/preloop/preloop) is the open-source AI agent
control plane (Apache-2.0, self-hostable, with
[Preloop Cloud](https://preloop.ai) as a hosted option). Once an agent's traffic
runs through it, alongside approvals you get:

- **MCP firewall.** Allow, deny, require approval, or require a justification on
  every MCP tool call, as YAML plus CEL policy.
- **AI model gateway.** OpenAI- and Anthropic-compatible, with per-agent
  budgets, allowed-model lists, and cost attribution. Provider keys stay with
  Preloop instead of inside agent containers.
- **Cost analytics and budgets.** Spend explained by model, agent, session, API
  key, and user, with soft and hard budget ceilings and budget-health alerts.
- **Session cost optimization.** Evidence-grounded waste findings per session,
  one-click apply, and consent-gated replay verification of the savings. This
  ships in the open-source core, using your own model keys.
- **Runtime session observability.** One timeline per session covering tool
  calls, model calls, policy decisions, approvals, and spend.
- **Audit trails.** Durable records with the matched policy, approver, inputs,
  timestamps, and outcome.

![The Preloop cost view: estimated spend and token totals for the period, per-agent cost breakdown, and budget health against soft and hard ceilings](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/cost_page.png)

![The Preloop audit timeline: a live, filterable stream of model requests, runtime sessions, token counts, cost per call, and outcomes](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/audit_page.png)

The point of the combination: one control plane over every agent you run, not a
separate dashboard per runtime. Preloop works with any MCP-compatible agent,
including OpenClaw, Claude Code, Codex CLI, Cursor, Gemini CLI, Hermes and
OpenCode. The same OpenClaw, still running at full speed, but now something you
can see while it works, stop before it does damage, talk to from anywhere, and
account for afterwards.

**Editions:** *Preloop* is the open-source edition. *Preloop Cloud* is the
hosted service. *Preloop Enterprise* is the commercial self-hosted edition.

## Learn more

- Docs: [docs.preloop.ai](https://docs.preloop.ai), and the
  [OpenClaw integration guide](https://docs.preloop.ai/guide/integrations/openclaw/)
- Source: [github.com/preloop/preloop](https://github.com/preloop/preloop). This
  plugin lives in
  [`runtime-plugins/openclaw-preloop`](https://github.com/preloop/preloop/tree/main/runtime-plugins/openclaw-preloop)
- Video series:
  [Preloop on YouTube](https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt)
- Issues: [github.com/preloop/preloop/issues](https://github.com/preloop/preloop/issues)

Apache-2.0. Copyright (c) 2026 Spacecode AI Inc.
