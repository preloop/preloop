# AI Model Gateway

The Preloop **Model Gateway** centralizes model traffic from managed runtimes. Instead of handing provider API keys to every local agent container, Preloop keeps credentials in its secret store and issues short-lived gateway tokens to enrolled runtimes.

---

## Why Use the Gateway

| Direct provider access | Through Preloop Gateway |
|------------------------|-------------------------|
| Raw API keys on every machine | Short-lived runtime tokens |
| Spend scattered across providers | Canonical `ApiUsage` ledger with attribution |
| No per-agent model limits | Subject-scoped allowed-model lists |
| Hard to audit prompts/responses | Normalized gateway events and session drill-down |

The gateway is the companion to the MCP Safety Layer: tools go through `/mcp/v1`, model calls go through `/openai/v1`, `/anthropic/v1`, or `/gemini/v1`.

---

## Supported Endpoints

Preloop exposes provider-compatible ingress on the gateway service:

| Path | Purpose |
|------|---------|
| `GET /openai/v1/models` | Model listing for OpenAI-compatible clients |
| `POST /openai/v1/chat/completions` | Chat completions with SSE streaming |
| `POST /openai/v1/responses` | Responses API with SSE streaming |
| `POST /anthropic/v1/messages` | Anthropic-format clients such as Claude Code |
| Gemini-compatible routes under `/gemini/v1beta` | Google/Gemini clients when configured |

Authentication reuses the same runtime bearer tokens issued during managed agent onboarding. Preloop preserves API-key context and runtime-principal metadata for attribution.

The OpenAI-compatible surface also serves clients you point at the gateway by hand with an account API key. Cursor is the main example: its Override OpenAI Base URL setting routes the AI panel's third-party model calls (Ask/Plan and Agent modes) through `/openai/v1`, with alias resolution serving Claude and Gemini models over the same path. Tab autocomplete, inline edit, and Cursor-billed bundled models stay on Cursor's backend and cannot be routed through any gateway. See [Cursor model routing](../clients/other-mcp-clients.md#route-cursor-model-traffic-through-preloop).

---

## Subscription OAuth Passthrough

If you use Claude Code with a **Pro/Max subscription** instead of an API key, Preloop can still route that traffic through the gateway. The credential type is a Claude Code OAuth token stored on the AI model, and the gateway treats it specially:

- **Byte-faithful proxying.** Anthropic requires the exact Claude Code request shape (the sentinel system block first, `cache_control` markers intact) and rejects anything else. So the gateway forwards `system`, `messages`, and nested `cache_control` blocks untouched, only the model identifier and streaming flag are set by Preloop, and SSE streams are relayed verbatim.
- **Still governed and budgeted.** Budget preflight, governance tool-stripping, attribution, and usage recording run exactly as on the normal path. Only the message bytes are sacred.
- **No context optimization on this traffic.** Message-level context optimizations are deliberately not applied: rewriting blocks would break byte-fidelity and destroy the prompt-cache prefix. See [Session Optimization](../cost/session-optimization.md#what-optimization-cannot-do-subscription-oauth-traffic).
- **The credential does not leak to other agents.** The OAuth token lives on one AI model in your account, and gateway calls resolve models per authenticated subject with per-agent attribution: another agent cannot ride a Claude Code subscription credential.
- **Validation is surfaced honestly.** Onboarding fires a live validation prompt. If the provider throttles it or refuses on billing/quota grounds, the agent stays enrolled but shows an **unverified** badge in the console (and a Reason in `preloop agents list`/onboarding summaries) until `preloop agents validate <agent> --live` passes.

---

## What Gets Recorded

Every gateway request can persist:

- token usage and estimated cost in `ApiUsage`
- provider, model alias, managed agent, runtime session, flow, and API key attribution
- normalized `model_gateway_call` events with redaction-aware payload capture (see [Redaction](../../security/redaction.md) for what is stored and how to store less)
- provider-neutral conversation previews for operator review

The console **Cost**, **Runtime Sessions**, and **AI Models** views read from this ledger.

---

## Budget and Allowed Models

Before dispatching upstream, Preloop evaluates:

1. **Account-level** gateway budgets
2. **Flow-level** budgets when the call originates from a flow execution
3. **Subject-scoped** allowed-model lists on the active API key or managed agent

Soft limits can annotate responses; hard limits deny the call before it reaches the provider.

Preloop Cloud and Preloop Enterprise add configurable budget policies, negotiated price overrides, and enforcement workflows through billing plugins. Preloop, the open-source edition, includes spend summaries and budget-health alerts from gateway limits. See [Enterprise Billing & FinOps](../integrations/enterprise-billing.md).

---

## Managed Agent Onboarding

When you run `preloop agents onboard openclaw` or `preloop agents onboard hermes`, the CLI attempts to rewrite local model configuration so traffic targets the Preloop gateway URL instead of direct provider endpoints. MCP configuration is narrowed to the managed Preloop MCP entry at the same time.

If model rewriting is unsupported for a particular runtime build, MCP governance can still succeed while model traffic needs a manual follow-up edit.

---

## Latency

Measured hop vs calling the model directly (self-hosted OSS, latency ping,
not a soak): [Gateway overhead](gateway-overhead.md). HTTP to the gateway
port is the Preloop process. Public HTTPS adds the reverse proxy and TLS.

## Related

- [Gateway overhead](gateway-overhead.md)
- [Subject-Scoped Governance](subject-scoped-governance.md)
- [Runtime Sessions](runtime-sessions.md)
- [Cost Analytics & Budgets](cost-analytics.md)
- [OpenClaw integration](../integrations/openclaw.md)
- [Hermes reference](../hermes.md)
