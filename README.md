# <img alt="Preloop Logo" src="frontend/public/assets/preloop-badge.png" style="height: 22px;" height="22px" /> Preloop - The Open-Source AI Agent Control Plane

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Preloop is the open-source AI agent control plane.** It unifies an **MCP firewall** for tool access, an **AI model gateway** for cost, safety and attribution, **policy-as-code** with **human approvals**, **runtime session observability**, and **audit trails** - in a single self-hostable platform.

Use Preloop to **onboard existing agents** with one command, and to **deploy event-driven agentic automations** with governed tools and budgets.

**Works with [OpenClaw](https://github.com/openclaw/openclaw), Claude Code, Codex CLI, Cursor, Gemini CLI, [Hermes](https://github.com/nousresearch/hermes-agent), [OpenCode](https://github.com/sst/opencode), Windsurf, and any MCP-compatible agent or managed runtime.**

Run `preloop agents discover` and Preloop will find local agent configs, import representable MCP servers and model metadata, mint managed runtime credentials, and rewrite supported agents to route tool calls through the **Preloop MCP Firewall** and model traffic through the **Preloop Gateway**. For Agent Control, the CLI provisions the credential/config contract and can delegate plugin installation to the runtime marketplace, but the plugin is what keeps the live control channel connected.


Build automations with templates like the [Pull Request Reviewer](./backend/presets/002-pull-request-reviewer.yaml), or write your own.

> **Official documentation:** Full guides and tutorials at [docs.preloop.ai](https://docs.preloop.ai).

```bash
# Install the standalone CLI
curl -fsSL https://preloop.ai/install/cli | sh
```

<p align="center">
  <img alt="Preloop onboarding local agents into the control plane" src="frontend/public/assets/screenshots/quickstart/dark/agents-onboarding.webp" style="width: 100%; max-width: 1135px; border-radius: 12px;" />
</p>

## What is Preloop?

Preloop is a single open-source platform that covers the five jobs teams otherwise buy from four different vendors:

| Capability | What it does | Alternatives |
|---|---|---|
| **MCP Firewall** | Govern every tool call an agent makes. Allow, deny, require approval, require justification. YAML + CEL policies. | MintMCP, Lunar.dev MCPX, TrueFoundry MCP Gateway |
| **AI Model Gateway** | OpenAI- and Anthropic-compatible gateway with per-account/flow budgets, allowed-model lists, token accounting, and runtime attribution. | Portkey, Helicone, LiteLLM, Kong AI |
| **Cost Analytics & Budgets** | Explain model spend by model, agent, session, API key, flow, and user; enforce budgets and inspect optimization opportunities. | FinOps dashboards, vendor billing exports |
| **Human Approvals** | Mobile, watch, Slack, Mattermost, email, or webhook notifications with one-tap decisions and full context. Async-safe. | Custom Slack bots, Peta Desk |
| **Runtime Observability** | Session-level timeline of tool calls, model calls, policy decisions, approvals, spend, and outcomes across agents. | AgentOps, Langfuse, LangSmith |
| **Audit & AI Act Evidence** | Durable logs with matched policy, approver, inputs, timestamps, and outcome. Ready for security review and EU AI Act work. | Credo AI, IBM watsonx.governance |

All shipped as Apache 2.0 software that runs on your infrastructure.

## Why Preloop?

AI agents like Claude Code, Cursor, and OpenClaw are transforming how we work. But agents now deploy code, touch production data, change infrastructure, and spend money — and traditional IAM, prompt rules, and manual review were never built for that.

- **Accidental deletions.** One wrong command and your production database is gone.
- **Leaked secrets.** API keys pushed to public repos before anyone notices.
- **Runaway costs.** Agents spinning up expensive cloud resources without limits.
- **Breaking changes.** Untested deployments to production at 3am.

Most teams face an impossible choice: give AI full access and move fast (but dangerously), or lock everything down and lose the productivity gains.

**Preloop solves this.** Govern what agents are allowed to do, route risky actions to the right human, attribute model spend to the right team, and keep a searchable record of every important decision — without rebuilding your stack or instrumenting SDKs.

```text
AI Agent → Preloop → [Policy check] → Allow / Deny / Require Approval → Execute
                   → [Gateway]       → Budget + attribution             → Model
```

## Core Capabilities

### Managed Agent Onboarding (`preloop agents discover`)
One command discovers and enrolls existing local agents into your control plane.

```bash
preloop agents discover
```

Preloop inspects local configurations for **Claude Code**, **Codex CLI**, **Cursor**, **Gemini CLI**, **[Hermes](https://github.com/nousresearch/hermes-agent)**, **[OpenClaw](https://github.com/openclaw/openclaw)**, **[OpenCode](https://github.com/sst/opencode)**, and other MCP-compatible runtimes, imports representable MCP servers and model metadata into your account, mints a durable credential, backs up the existing config, and rewrites supported local endpoints to Preloop-managed MCP and gateway URLs. Legacy and current config locations are supported, JSON5/YAML parsing included.

Managed onboarding has two layers: CLI provisioning and runtime behavior. The CLI can create credentials, write `preloop.control` configuration, and invoke runtime-native plugin installation where the target runtime supports that workflow. It cannot, by itself, make an unmodified agent process stay online for Agent Control. Live OpenClaw/Hermes Agent Control requires the standalone Preloop runtime plugin to be loaded in the agent process.

To make Talk appear for OpenClaw or Hermes, the agent must be active, have a
`preloop.control` block with a valid runtime bearer token, and have the Preloop
runtime plugin online. The normal CLI path is:

```bash
preloop agents onboard openclaw
preloop agents install-plugin openclaw
preloop agents validate openclaw

preloop agents onboard hermes
preloop agents install-plugin hermes
preloop agents validate hermes
```

After installing the plugin, restart the agent runtime. When the plugin connects
to `WS /api/v1/agents/control/ws` and advertises capabilities, Preloop marks the
Agent Control channel verified and the web/mobile Talk controls become available.
The plugin-only path uses the same contract: install `@preloop/openclaw-plugin`
or `preloop-hermes-plugin`, provide a valid `preloop.control` block, start the
runtime, and let the plugin connect.

<p align="center">
  <img alt="Preloop dashboard with live agent and gateway usage" src="frontend/public/assets/screenshots/quickstart/dark/dashboard.png" style="width: 100%; max-width: 1135px; border-radius: 12px;" />
</p>

### Access Policies & Approval Workflows
Define fine-grained access controls for any AI tool or operation. Tools support multiple ordered access rules that evaluate in priority order. When an AI attempts a protected operation, Preloop pauses and notifies you:
- **Instant notifications** via mobile app, email, Slack, Mattermost, or custom webhook.
- **One-tap approvals** from your phone, watch, or desktop.
- **Async approval mode** lets the agent poll for status instead of blocking network hooks.
- **Per-tool justification** — require (or optionally request) the agent to explain *why* a tool is being called.
- **Full Audit Trail** — every action is logged with full context: what was attempted, the matched policy, execution duration, and who approved it.

<div align="center">
  <img alt="Preloop MCP tool policy rules configured for an example pay tool" src="frontend/public/assets/screenshots/quickstart/dark/rules_configured.png" style="width: 49%; min-width: 320px; border-radius: 12px; margin-right: 1%;" />
  <img alt="Preloop audit log showing governed agent activity" src="frontend/public/assets/screenshots/quickstart/dark/audit_page.png" style="width: 49%; min-width: 320px; border-radius: 12px; margin-left: 1%;" />
</div>

### Policy-as-Code
Define policies in YAML and manage via CLI or API to version-control your safeguards alongside your infrastructure:

```yaml
# Example: Require approval for production deployments
version: "1.0"
metadata:
  name: "Production Safeguards"
  description: "Require approval before deploying"

approval_workflows:
  - name: "deploy-approval"
    timeout_seconds: 600
    required_approvals: 1
    async_approval: true

tools:
  - name: "bash"
    source: mcp
    approval_workflow: "deploy-approval"
    justification: required
    conditions:
      - expression: "args.command.contains('deploy') && args.command.contains('production')"
        action: require_approval
```

### AI Model Gateway
Preloop safely routes model traffic on behalf of managed runtimes instead of handing provider credentials to potentially vulnerable agent containers.
- **OpenAI-compatible** (`/openai/v1/models`, `/openai/v1/chat/completions`, `/openai/v1/responses`) and **Anthropic-compatible** (`/anthropic/v1/messages`) endpoints with SSE streaming.
- **Budget enforcement** at account, flow, and subject scopes using configurable cost tracking limits.
- **Allowed-model lists** per account, flow, API key, or managed agent.
- **Usage accounting** persisted as a canonical `ApiUsage` ledger — token usage, estimated cost, runtime-principal attribution, and provider-neutral conversation previews.
- **Secret custody** — provider API keys stay with Preloop; runtimes receive short-lived gateway tokens instead of raw credentials.

### Cost Analytics & Budgets
Preloop should make model spend explainable, not just counted. The Console should have a dedicated **Cost** area with subviews that help operators answer:

- **How much have we spent?** Spend, token volume, and request counts over time by model, provider, account, flow, managed agent, runtime session, API key, and user.
- **Who or what spent it?** Attribution from `ApiUsage`, runtime principals, subject-scoped governance, and session timelines.
- **Why was it spent?** Drill-down from aggregate charts into session transcripts, model-call previews, tool calls, approvals, and flow outcomes.
- **Was the outcome worth it?** Enterprise analysis can use the account's default AI model to summarize sessions, compare spend against observed outcome, and flag low-value or failed runs.
- **How could it be optimized?** Enterprise recommendations can suggest cheaper models, prompt reductions, caching, batching, retry suppression, budget policy changes, or approval gates for expensive workflows.

The open-source edition should include a practical Cost Overview, usage drill-downs, and budget-health alerts from gateway account/flow limits. Enterprise Edition adds budget policy configuration and enforcement, negotiated provider pricing overrides, session optimization recommendations, FinOps workflows, AI-generated session value reviews, anomaly detection, chargeback/showback, credits and promotions, forecasting, approval escalations, and export/reporting features through backend plugins. The frontend remains shared and should expose advanced panels through feature flags.

### Runtime Session Observability
A durable `RuntimeSession` layer gives you one timeline per managed runtime — flow executions today, and any onboarded CLI/desktop agent session going forward. Operator-scoped endpoints expose recent sessions plus captured gateway interactions so the console can drill from aggregate usage into a single session timeline. Operators can end a session explicitly; doing so updates runtime state, emits audit events, and refreshes managed-agent summaries.

### Agent Control
Preloop is evolving from an approval and gateway layer into **Agent Control** for long-running autonomous agents.

- **Implemented today:** account-scoped realtime topics, runtime-session identity, managed-agent enrollment, OpenClaw and Hermes config rewrites, MCP proxying, model-gateway routing, session timelines, operator lifecycle actions, `WS /api/v1/agents/control/ws`, and `POST /api/v1/agents/{agent_id}/control/commands`.
- **Scaffolded today:** console views for runtime sessions and managed agents, mobile/watch approval clients, WebSocket delivery for account events and approval updates, and native voice/dictation surfaces that can submit operator text turns.
- **Runtime-plugin dependent:** live OpenClaw and Hermes command delivery requires their native runtime plugin to honor `preloop.control.control_ws_url`, own reconnect/backoff and heartbeat/status loops, advertise capabilities, receive `send_message` envelopes, execute or inject operator messages into the active agent session, and keep any resulting tool/model work on the governed MCP and gateway paths. Without that plugin loaded, the agent can still be onboarded for MCP/gateway routing, but Agent Control is not enabled.
- **Voice surfaces:** the web console now supports inline Talk controls with browser-native STT/TTS first and server STT/TTS fallback through speech-capable `AIModel` rows. Mobile/watch apps should continue to prefer vendor-native speech APIs and submit normalized voice transcripts through the same Agent Control surface.
- **Planned next:** hardened command persistence/recovery, interruption semantics, richer status streaming, and production voice UX on mobile and watch.

Agent Control surfaces are intentionally layered: the web console is the primary operator surface for managed-agent presence, session context, and text commands; mobile apps add push-to-talk or typed handoff for urgent operator messages; Apple Watch favors short dictated replies and quick status/approval actions.

Agent Control messages are auditable user/operator turns for a selected runtime session. They are not hidden system prompts or policy overrides; any tool or model work they trigger still flows through the MCP firewall, model gateway, budget checks, and approval policies.

Siri and Google Assistant should be treated as invocation and handoff surfaces, not reliable arbitrary background agent transports. Native app code can capture intent, permissions, and user confirmation; Preloop keeps the authoritative control, audit, and agent-message state on the server.




## Getting Started

Choose the path that matches what you want to evaluate:

- **Fast public trial:** deploy the self-contained Railway trial template. This gives you a public Preloop URL without manually provisioning a VM.
- **Local laptop:** install the OSS stack with the install script.
- **Kubernetes/prod-like:** use the Helm chart in [`helm/preloop`](helm/preloop).

### Try Preloop OSS in 5 minutes

[![Deploy on Railway](https://railway.com/button.svg)](deploy/railway/README.md)

The Railway trial runs Preloop Console, API/gateway, worker/scheduler, Postgres with pgvector, and NATS in one Railway project. The default template is self-contained and does not depend on external managed databases or queues. It is intended for evaluation, not hardened production.

Until the public Railway template code is published, the button opens the checked-in template guide and service map in [`deploy/railway`](deploy/railway). After publishing, replace the link target with the Railway template URL.

### Install locally

```bash
# Install the standalone CLI
curl -fsSL https://preloop.ai/install/cli | sh

# Install the OSS platform stack
curl -fsSL https://preloop.ai/install/oss | sh
```

### Release smoke test for hosted trials

Before promoting a hosted trial template, verify that the public URL loads the console, `/api/v1/health` responds, first-user sign-in or sign-up works, `preloop agents discover` can target the public URL, one gateway model call appears in the UI, and one MCP policy event appears in the audit timeline.

For extended details detailing comprehensive Docker builds, Kubernetes Helm topologies, GraphQL configuration, WebSocket streaming channels, and deep `.env` definitions, refer to the [Preloop Documentation Hub](https://docs.preloop.ai).

> **Production requirement:** The `SECRET_KEY` environment variable is **required** in production. Without it, the application will refuse to start. In development, a default key is used with a warning. Generate a secure key with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

## The Open-Source Alternative to AWS Bedrock AgentCore

Preloop covers the same core jobs as AWS Bedrock AgentCore (runtime, gateway, identity, observability, policy) but is open source, self-hostable, MCP-native, and vendor-neutral. Many teams adopt Preloop specifically as an **open-source alternative to AWS Bedrock AgentCore** when they want to avoid hyperscaler lock-in or need to run governance inside their own VPC or on-prem.

| Feature | Preloop | AWS Bedrock AgentCore |
|---------|:-------:|:--------------:|
| Open source (Apache 2.0) | ✅ | ❌ |
| Self-hostable (VPC / on-prem) | ✅ | ❌ |
| Policy-as-code (YAML + CEL) | ✅ | Limited |
| MCP-native tool governance | ✅ | Partial |
| Model gateway with budgets & attribution | ✅ | ✅ |
| Human-in-the-loop approval workflows | ✅ (mobile, Slack, webhook) | Limited |
| Works with any agent runtime | ✅ | AWS-centric |
| Vendor lock-in | None | AWS |
| Onboard existing local agents with one command | ✅ (`preloop agents discover`) | ❌ |

## How Preloop Compares to Other Categories

| Category | Common tools | How Preloop differs |
|---|---|---|
| **AI gateways / LLM proxies** | Portkey, Helicone, LiteLLM, Kong AI | Preloop's gateway is bundled with an MCP firewall, approval workflows, and runtime observability — you do not need to stitch four products together. |
| **MCP gateways** | MintMCP, Lunar.dev MCPX, TrueFoundry | Preloop is open-source and includes a first-class AI model gateway, not just MCP tool routing. |
| **AgentOps / observability** | Langfuse, LangSmith, Braintrust, AgentOps.ai | Preloop adds runtime *enforcement* (policy, approvals, budgets), not just tracing. |
| **AI runtime security** | Lakera, Lasso, Zenity, Noma | Preloop is developer-facing, MCP-native, and self-hostable. Complementary to semantic content-safety firewalls. |
| **AI governance suites** | Credo AI, IBM watsonx, OneTrust | Preloop focuses on runtime controls agents actually hit, not just top-down inventory and risk artifacts. |

## Enterprise Features

Preloop Enterprise Edition extends the core open-source components with centralized RBAC capabilities:

| Feature | Open Source | Enterprise |
|---------|:-----------:|:----------:|
| Basic approval workflows | ✅ | ✅ |
| Issue tracker integrations | ✅ | ✅ |
| Agentic flows & Vector search | ✅ | ✅ |
| **Cost overview, usage drill-downs & budget-health tracking** | ✅ | ✅ |
| **Budget policy configuration & enforcement** | ❌ | ✅ |
| **Per-account model price overrides** | ❌ | ✅ |
| **Role-Based Access Control (RBAC)** | ❌ | ✅ |
| **Team management & Admin Dashboard** | ❌ | ✅ |
| **CEL conditional approval workflows** | ❌ | ✅ |
| **AI-driven approval logic** | ❌ | ✅ |
| **Team-based approvals with quorum** | ❌ | ✅ |
| **Approval escalation** | ❌ | ✅ |
| **AI session value reviews & spend optimization recommendations** | ❌ | ✅ |
| **Credits, promotions, chargeback/showback & forecasting** | ❌ | ✅ |

Contact sales@preloop.ai for Enterprise Edition licensing requests.

## Contributing

Contributions are welcome! Please see our [Contributing Guidelines](CONTRIBUTING.md) for details on how to get started.

## License

Preloop is open source software licensed under the [Apache License 2.0](LICENSE).
Copyright (c) 2026 Spacecode AI Inc.
