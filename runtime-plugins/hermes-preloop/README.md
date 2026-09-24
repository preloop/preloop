# Govern your Hermes agent: approvals on your phone, spend you can see

Your agent decides to run `kubectl delete deployment api`. You are not at the
keyboard. Right now it just runs.

`preloop-hermes-plugin` connects
[Hermes](https://github.com/nousresearch/hermes-agent) to
[Preloop](https://github.com/preloop/preloop), the open-source AI agent control
plane. With the plugin installed, that command pauses, the request lands on your
phone, watch, Slack, Mattermost, email, or the web console, and you tap
**Approve** or **Deny**. The agent continues or gets blocked with your reason.
Everything it did is recorded.

## What you get

- **Dangerous commands wait for you instead of just running.** Hermes has no
  local allow or deny lists for its native tools, so every single tool call goes
  to your central policy. Allow, deny, or hold for a human, per tool and per
  argument.
- **Approve from wherever you actually are.** Push to phone or watch, Slack,
  Mattermost, email, an outbound webhook, or the Preloop console. The tool call
  stays blocked while you decide, for up to 24 hours by default, so "I will look
  at it after lunch" is a valid answer.
- **Nothing runs ungoverned when Preloop is down.** The gate fails closed by
  default. An unreachable control plane, a 5xx, a bad token, a malformed reply
  or malformed YAML in your config all block the call rather than waving it
  through.
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

- **The plugin** (this package) gates the tools Hermes reaches for, the shell
  commands and file writes the model decides to run, and holds open the control
  channel for messages and interrupts.
- **Onboarding Hermes to Preloop** (one extra CLI command, below) routes its MCP
  and model traffic through Preloop, which is what produces per-agent cost
  attribution, budgets, allowed-model lists, and session cost optimization. The
  plugin does not do this on its own.

Install the plugin and you get governance. Onboard the agent and you also get
the bill, itemized, across every agent you run rather than just Hermes.

It is Apache-2.0, and it works against either the open-source
[Preloop](https://github.com/preloop/preloop) control plane you host yourself,
or the hosted [Preloop Cloud](https://preloop.ai).

**Watch it work.** Onboarding, tool governance, approvals, and cutting session
cost, recorded end to end against a real stack:

[![Preloop video series: see your agents, govern them, cut their cost](https://img.youtube.com/vi/Y_geb2Or8zM/maxresdefault.jpg)](https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt)

## 60 seconds to install

Python **3.11+** and Hermes **v0.10.0 or newer** are required. The version floor
is load-bearing for the approval gate. Read [Hermes version](#hermes-version)
below before you rely on it.

```bash
pip install preloop-hermes-plugin
```

If your Hermes build wraps PyPI installs:

```bash
hermes plugins install preloop-hermes-plugin
```

Hermes discovers the plugin through the `hermes_agent.plugins` entry point after
install. Restart Hermes afterwards.

You also need a Preloop control plane to approve against, either
[Preloop Cloud](https://preloop.ai) (nothing to run) or the open-source stack on
your own machine:

```bash
curl -fsSL https://preloop.ai/install/oss | sh
```

### Connect it

The plugin ships its own login helper, so you never hand-author a token:

```bash
preloop-hermes-plugin login
preloop-hermes-plugin verify
```

`login` opens Preloop's OAuth flow, mints a runtime bearer token, and writes the
`preloop.control` block into the discovered config file (backing up the existing
file alongside it first). `verify` checks the config shape and that the plugin
loads.

Config discovery order: `--config <path>` (if given), then
`$HERMES_HOME/config.yaml` and `config.yml`, then `~/.hermes/config.yaml` and
`config.yml`. When several of those files exist, the plugin prefers the first
that contains a `preloop.control` block, so a systemd user unit with a
different `HERMES_HOME` does not mask the file onboarding wrote. When no
config file exists yet, `$HERMES_HOME/config.yaml` is preferred if the
variable is set so the new file lands where Hermes will read it. Pass
`--config` explicitly to override.

### Or let the Preloop CLI do all of it

If you have (or want) the [Preloop CLI](https://docs.preloop.ai), it discovers
your Hermes install, backs up the config, installs this plugin, and writes the
credentials for you:

```bash
curl -fsSL https://preloop.ai/install/cli | sh
preloop signup                        # or: preloop login --url http://localhost:3000
preloop agents onboard hermes
preloop agents install-plugin hermes
preloop agents validate hermes
```

This is the path that also routes Hermes' MCP tool calls through the Preloop MCP
firewall and its model traffic through the Preloop gateway, which is where the
budgets and per-agent cost attribution come from. The plugin alone covers native
tool approvals and the control channel.

Undo anything with `preloop agents restore hermes` or
`preloop agents offboard hermes`.

### Hermes version

The gate works by returning `{"action": "block", ...}` from a `pre_tool_call`
plugin hook. Hermes only started acting on that return value in
[v0.10.0](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.4.16)
(tag `v2026.4.16`). Before that, `pre_tool_call` callbacks were observer-only
and their return value was discarded.

So on Hermes **v0.9.0 and older** this plugin installs cleanly, connects to
Preloop, shows the agent as online, and gates nothing. Tool calls run unchecked
while the console looks healthy. There is no error to notice.

Check what you are running:

```bash
hermes --version
```

If you need to confirm the enforcement path directly rather than trust the
version string:

```bash
python3 -c "import hermes_cli.plugins as p; print(hasattr(p, 'get_pre_tool_call_block_message'))"
```

`True` means the build acts on block directives. Newer builds route the same
decision through `resolve_pre_tool_block`, which also carries the
`{"action": "approve"}` escalation Hermes added in
[v0.18.1](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.7.7).
This plugin does not depend on that path, because approvals are decided in
Preloop and returned as a block, not handed to Hermes' local prompt.

## What happens on the first dangerous tool call

The model picks a tool. Before Hermes runs it, the plugin's `pre_tool_call` hook
stops it and works through three steps.

1. **Preloop evaluates your central policy.** Unlike some runtimes, Hermes has
   no local allow or deny lists for native tools, so the plugin escalates every
   call rather than resolving anything itself. Rules match on the tool name and
   its arguments, written either as simple conditions or as CEL expressions, and
   the whole policy can be exported and imported as YAML. A rule can allow the
   call, deny it, or require a human. Most calls resolve here without bothering
   anyone.
2. **If a human is required, the request goes out and the tool waits.** A
   notification with the full command lands on your phone, watch, Slack,
   Mattermost, email, an outbound webhook, or the console. The tool call is
   still blocked while you think. The default wait budget is 24 hours.
3. **The answer comes back and is recorded.** Approve and the tool runs. Deny
   and Hermes receives a block envelope carrying the reason you gave, which the
   agent sees as the tool result and usually works around. Either way the
   decision is written to the same audit trail as your MCP tool calls.

**When things go wrong, the call blocks.** Only a valid `allow` lets a tool run.
A returned `deny`, including an approval that expired (`timed_out: true`),
blocks even if you turned fail-open on. Transport errors, timeouts and HTTP 5xx
block by default and are the only failures that an explicit fail-open can waive.
HTTP 4xx (including 401 and 403), malformed decisions, missing credentials,
unreadable config and invalid configuration always block. `enabled` and
`fail_open` must be real YAML booleans; a quoted string such as `"false"` is
rejected and never opts you into ungoverned execution.

![The Preloop tools page with rules on an MCP tool: deny above one threshold, require approval in the middle band, allow below it, each rule written as a CEL expression over the tool arguments](https://raw.githubusercontent.com/preloop/preloop/main/frontend/public/assets/screenshots/quickstart/dark/rules_configured.png)

## Configuration reference

The plugin reads the `preloop.control` block from the discovered Hermes config
(`$HERMES_HOME/config.yaml` or `.yml`, then `~/.hermes/config.yaml` or `.yml`;
see [Connect it](#connect-it)):

```yaml
preloop:
  control:
    enabled: true
    protocol: preloop.agent_control.v1
    runtime: hermes
    control_ws_url: wss://app.preloop.ai/api/v1/agents/control/ws
    bearer_token: agt_...
    runtime_principal_id: hermes-...
    runtime_principal_name: Hermes
    tool_approval:
      enabled: true
      fail_open: false
```

| Setting | Default | What it does |
|---|---|---|
| `tool_approval.enabled` | `true` | Set to `false` to turn the native tool-call gate off entirely |
| `tool_approval.fail_open` | `false` | Fail-closed by default: if Preloop is unreachable, the tool call is **blocked**. Set `true` only if you accept ungoverned execution during an outage |
| `tool_approval.timeout_seconds` | `86400` | Workflow wait budget, an integer from 30 to 86400 seconds; HTTP adds 15 seconds and the synchronous hook bridge adds another 15 seconds |
| `PRELOOP_TOOL_APPROVAL_FAIL_OPEN` | unset | Environment override for `fail_open` (`1`/`true`/`yes`/`on`) |
| `PRELOOP_DESKTOP_FILE` | `~/.preloop/desktop.json` | Desktop manifest to read for the Agent Control `desktop` capability. A parsed file whose `vnc.host` is exactly `127.0.0.1` advertises `desktop: vnc` and `desktop_display`. The VNC password file is never read |

### Wait budgets

The default budget covers approval workflows up to 24 hours, including a
workflow selected by a central rule. It does not change the workflow expiry: a
five-minute workflow still expires after five minutes. A shorter configured
budget must cover every workflow this agent can select, because reaching the
HTTP or bridge deadline follows the failure behaviour above. Missing config uses
the default; invalid values block and are rejected by `verify`.

Bundled standalone and Helm nginx configurations allow 86460 seconds only on
`/api/v1/agents/permission-check`. Other ingress, load-balancer and Hermes host
limits must also permit the chosen wait.

Disabling the plugin gate skips central policy. Preloop's server-side
approvals-off setting only disables human escalation and does not bypass an
explicit central require-approval rule.

## Manual Test Without Preloop CLI

The plugin does not need the Preloop CLI at runtime. To check an install by
hand:

```bash
pip install preloop-hermes-plugin
preloop-hermes-plugin login          # or: --config /path/to/config.yaml
preloop-hermes-plugin verify
preloop-hermes-plugin run
```

`run` opens the Agent Control WebSocket and advertises capabilities without
Hermes attached. In Preloop the agent should show as online, and Talk controls
should appear in the console and mobile apps. To test message delivery end to
end, run the plugin inside Hermes itself (not via `run`, which has no session
attached), then pick the Hermes agent in the console, click Talk, and send a
short message.

## What the plugin actually does

Scoped honestly, so you know what you are installing:

- Maintains a WebSocket to Preloop's Agent Control endpoint, with reconnect
  backoff and heartbeat, so the channel survives laptop sleep and network
  changes.
- Advertises capabilities: new and existing sessions, text, voice transcripts,
  interrupt, tool approval.
- Delivers operator messages and voice transcripts into the running Hermes
  session, and relays interrupts.
- Gates every native tool call through `pre_tool_call`, fail-closed, bridging
  Hermes' synchronous hook onto its own event loop so the decision is a plain
  dict by the time Hermes reads it.

What it does **not** do: it is not a content filter or a prompt-injection
defense. Preloop's protection against prompt injection is partial. Policy and
approvals mean an injected instruction still has to get past your rules and, for
anything risky, past you. That is a meaningful barrier, not a guarantee. It does
not see Hermes' MCP tool calls at all: those are governed by the MCP firewall,
which requires onboarding. It does not itself route model traffic, and so it
does not by itself produce cost attribution, budgets, or optimization findings.
And it cannot enforce anything the host runtime does not enforce for it: the
gate is only as good as Hermes' `pre_tool_call` handling, which is why the
[version floor](#hermes-version) above is a hard requirement rather than a
recommendation.

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
including Hermes, OpenClaw, Claude Code, Codex CLI, Cursor, Gemini CLI and
OpenCode. The same Hermes, still running at full speed, but now something you
can see while it works, stop before it does damage, talk to from anywhere, and
account for afterwards.

**Editions:** *Preloop* is the open-source edition. *Preloop Cloud* is the
hosted service. *Preloop Enterprise* is the commercial self-hosted edition.

## Learn more

- Docs: [docs.preloop.ai](https://docs.preloop.ai), the
  [Hermes reference](https://docs.preloop.ai/guide/clients/hermes/), and the
  in-repo [Hermes recovery guide](../../docs/guide/hermes.md)
- Source: [github.com/preloop/preloop](https://github.com/preloop/preloop). This
  plugin lives in
  [`runtime-plugins/hermes-preloop`](https://github.com/preloop/preloop/tree/main/runtime-plugins/hermes-preloop)
- Video series:
  [Preloop on YouTube](https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt)
- Issues: [github.com/preloop/preloop/issues](https://github.com/preloop/preloop/issues)

Apache-2.0. Copyright (c) 2026 Spacecode AI Inc.
