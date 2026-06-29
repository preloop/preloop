# <img alt="Preloop Logo" src="frontend/public/assets/preloop-badge.png" style="height: 22px;" height="22px" /> Preloop — The Open-Source AI Agent Control Plane

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docs](https://img.shields.io/badge/docs-docs.preloop.ai-7c3aed.svg)](https://docs.preloop.ai)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**Preloop is the open-source control plane for AI agents.** It puts an **MCP firewall** in front of every tool call, routes model traffic through an **AI gateway** with budgets and attribution, gates risky actions behind **human approvals**, and records a **searchable audit trail** of everything — in one self-hostable platform.

Onboard the agents you already run — **OpenClaw, Claude Code, Codex CLI, Cursor, Gemini CLI, [Hermes](https://github.com/nousresearch/hermes-agent), [OpenCode](https://github.com/sst/opencode), Windsurf**, and any MCP-compatible runtime — with **one command**. No SDKs. No agent code changes. Self-hostable. Apache-2.0.

> **The open-source alternative to AWS Bedrock AgentCore** — runtime, gateway, identity, observability, and policy, MCP-native and vendor-neutral. [See the comparison →](#how-preloop-compares)

---

## Quick start

Install the standalone CLI:

```bash
curl -fsSL https://preloop.ai/install/cli | sh
```

Point it at your Preloop instance and discover the agents already on your machine:

```bash
preloop login
preloop agents discover
```

`preloop agents discover` inspects your local agent configs, imports their MCP servers and model metadata, mints a managed credential, backs up each config, and rewrites supported agents to route through Preloop:

```text
$ preloop agents discover
Scanning ~/.claude, ~/.codex, ~/.cursor, ~/.gemini, ~/.openclaw ...
Found 5 local agents:
  ✓ Claude Code     → tool calls via MCP Firewall, models via Gateway
  ✓ Codex CLI       → tool calls via MCP Firewall, models via Gateway
  ✓ Cursor          → tool calls via MCP Firewall
  ✓ Gemini CLI      → models via Gateway
  ✓ OpenClaw        → Agent Control channel connected
5 agents onboarded · budgets, approvals & audit now enforced
```

That's it — tool calls now flow through the firewall and model calls through the gateway, with spend, approvals, and audit applied across your whole fleet. Run `preloop agents discover --json --no-onboard-prompt` to inspect without changing anything first.

> Don't have an instance yet? [Deploy the OSS stack](#deploy-the-platform) on your laptop, Railway, or Kubernetes — then come back to this step.

<p align="center">
  <img alt="Preloop onboarding local agents into the control plane" src="frontend/public/assets/screenshots/quickstart/dark/agents-onboarding.webp" style="width: 100%; max-width: 1135px; border-radius: 12px;" />
</p>

> **Full guides and tutorials:** [docs.preloop.ai](https://docs.preloop.ai)

## What is Preloop?

AI agents now deploy code, touch production data, change infrastructure, and spend money — but traditional IAM, prompt rules, and manual review were never built for that. Preloop is a single platform that covers the five jobs teams otherwise buy from four different vendors:

| Capability | What it does | Alternatives |
|---|---|---|
| **MCP Firewall** | Govern every tool call: allow, deny, require approval, require justification. YAML + CEL policies. | MintMCP, Lunar.dev MCPX, TrueFoundry |
| **AI Model Gateway** | OpenAI- and Anthropic-compatible gateway with per-account/flow budgets, allowed-model lists, token accounting, runtime attribution. | Portkey, Helicone, LiteLLM, Kong AI |
| **Cost Analytics & Budgets** | Explain model spend by model, agent, session, API key, flow, and user; enforce budgets. | FinOps dashboards, billing exports |
| **Human Approvals** | Mobile, watch, Slack, Mattermost, email, or webhook notifications with one-tap decisions. Async-safe. | Custom Slack bots |
| **Runtime Observability** | Session-level timeline of tool calls, model calls, policy decisions, approvals, spend, and outcomes. | AgentOps, Langfuse, LangSmith |
| **Audit & AI Act Evidence** | Durable logs with matched policy, approver, inputs, timestamps, and outcome. | Credo AI, IBM watsonx.governance |

All shipped as Apache-2.0 software that runs on your infrastructure:

```text
AI Agent → Preloop → [Policy check] → Allow / Deny / Require Approval → Execute
                   → [Gateway]       → Budget + attribution             → Model
```

## Core capabilities

### Managed agent onboarding (`preloop agents discover`)

One command discovers and enrolls existing local agents. Preloop inspects configs for **Claude Code**, **Codex CLI**, **Cursor**, **Gemini CLI**, **[Hermes](https://github.com/nousresearch/hermes-agent)**, **[OpenClaw](https://github.com/openclaw/openclaw)**, **[OpenCode](https://github.com/sst/opencode)**, and other MCP-compatible runtimes, imports representable MCP servers and model metadata, mints a durable credential, backs up the existing config, and rewrites supported endpoints to Preloop-managed MCP and gateway URLs. Legacy and current config locations are supported; JSON5/YAML parsing included.

For live **Agent Control** of OpenClaw or Hermes, install the runtime plugin and restart the agent:

```bash
preloop agents onboard openclaw
preloop agents install-plugin openclaw
preloop agents validate openclaw
```

When the plugin connects to `WS /api/v1/agents/control/ws` and advertises capabilities, the Agent Control channel is verified and the web/mobile Talk controls become available. The CLI can provision credentials and config for any supported agent, but the live control channel needs the runtime plugin loaded in the agent process.

### Access policies & approval workflows

Define ordered, priority-evaluated access rules for any MCP or built-in tool. When an agent hits a protected operation, Preloop pauses and notifies the right people:

- **Instant notifications** via mobile, watch, email, Slack, Mattermost, or webhook — **one-tap approvals**.
- **Async approval mode** lets the agent poll for status instead of blocking transport hooks.
- **Per-tool justification** — require the agent to explain *why* a tool is being called.
- **Full audit trail** — what was attempted, the matched policy, duration, and who approved it.

<div align="center">
  <img alt="Preloop MCP tool policy rules configured for an example pay tool" src="frontend/public/assets/screenshots/quickstart/dark/rules_configured.png" style="width: 49%; min-width: 320px; border-radius: 12px; margin-right: 1%;" />
  <img alt="Preloop audit log showing governed agent activity" src="frontend/public/assets/screenshots/quickstart/dark/audit_page.png" style="width: 49%; min-width: 320px; border-radius: 12px; margin-left: 1%;" />
</div>

### Policy-as-code

Version-control your safeguards alongside your infrastructure:

```yaml
# Require approval for production deployments
version: "1.0"
metadata:
  name: "Production Safeguards"

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

### AI model gateway

Preloop routes model traffic on behalf of managed runtimes instead of handing provider credentials to agent containers.

- **OpenAI-compatible** (`/openai/v1/models`, `/openai/v1/chat/completions`, `/openai/v1/responses`) and **Anthropic-compatible** (`/anthropic/v1/messages`) endpoints with SSE streaming.
- **Budget enforcement** at account, flow, and subject scopes.
- **Allowed-model lists** per account, flow, API key, or managed agent.
- **Usage accounting** persisted as a canonical `ApiUsage` ledger — tokens, estimated cost, runtime-principal attribution, and provider-neutral previews.
- **Secret custody** — provider keys stay with Preloop; runtimes receive short-lived gateway tokens.

### Cost analytics, observability & audit

A durable `RuntimeSession` layer gives one timeline per managed runtime. Drill from aggregate spend into a single session's tool calls, model calls, approvals, and outcomes. The open-source edition includes a practical Cost Overview, usage drill-downs, and budget-health alerts; build automations with templates like the [Pull Request Reviewer](./backend/presets/002-pull-request-reviewer.yaml), or write your own.

## Deploy the platform

Choose the path that matches what you want to evaluate:

```bash
# Local laptop — install the OSS platform stack
curl -fsSL https://preloop.ai/install/oss | sh
```

- **Fast public trial:** [![Deploy on Railway](https://railway.com/button.svg)](deploy/railway/README.md) — Console, API/gateway, worker/scheduler, Postgres+pgvector, and NATS in one self-contained project. For evaluation, not hardened production.
- **Kubernetes/prod-like:** use the Helm chart in [`helm/preloop`](helm/preloop).

> **Production requirement:** `SECRET_KEY` is **required** in production — the app refuses to start without it. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. In development a default key is used with a warning.

For comprehensive Docker builds, Kubernetes topologies, WebSocket channels, and `.env` definitions, see the [Documentation Hub](https://docs.preloop.ai).

## How Preloop compares

Preloop covers the same core jobs as **AWS Bedrock AgentCore** (runtime, gateway, identity, observability, policy) but is open source, self-hostable, MCP-native, and vendor-neutral — adopted by teams that want to avoid hyperscaler lock-in or run governance inside their own VPC.

| Feature | Preloop | AWS Bedrock AgentCore |
|---------|:-------:|:--------------:|
| Open source (Apache 2.0) | ✅ | ❌ |
| Self-hostable (VPC / on-prem) | ✅ | ❌ |
| Policy-as-code (YAML + CEL) | ✅ | Limited |
| MCP-native tool governance | ✅ | Partial |
| Model gateway with budgets & attribution | ✅ | ✅ |
| Human-in-the-loop approvals | ✅ (mobile, Slack, webhook) | Limited |
| Works with any agent runtime | ✅ | AWS-centric |
| Onboard existing local agents with one command | ✅ | ❌ |

Versus adjacent categories: AI gateways (Portkey, LiteLLM) route model traffic — Preloop bundles that with an MCP firewall, approvals, and observability. MCP gateways (MintMCP, Lunar.dev) route tools — Preloop adds a first-class model gateway. AgentOps tools (Langfuse, LangSmith) trace — Preloop adds runtime *enforcement*. See the [full breakdown in the docs](https://docs.preloop.ai).

## Enterprise Edition

Preloop Enterprise Edition extends the open-source core with centralized governance:

| Feature | Open Source | Enterprise |
|---------|:-----------:|:----------:|
| MCP firewall, model gateway, approvals, audit | ✅ | ✅ |
| Cost overview, usage drill-downs & budget-health | ✅ | ✅ |
| Budget policy configuration & enforcement | ❌ | ✅ |
| Per-account model price overrides | ❌ | ✅ |
| RBAC, team management & admin dashboard | ❌ | ✅ |
| CEL conditional & AI-driven approvals | ❌ | ✅ |
| Team-based approvals with quorum & escalation | ❌ | ✅ |
| AI session value reviews & spend optimization | ❌ | ✅ |
| Credits, promotions, chargeback/showback & forecasting | ❌ | ✅ |

Contact sales@preloop.ai for Enterprise Edition licensing.

## Contributing

Contributions are welcome! See our [Contributing Guidelines](CONTRIBUTING.md) to get started, and [ARCHITECTURE.md](ARCHITECTURE.md) for a tour of the codebase.

## License

Preloop is open source software licensed under the [Apache License 2.0](LICENSE).
Copyright (c) 2026 Spacecode AI Inc.
