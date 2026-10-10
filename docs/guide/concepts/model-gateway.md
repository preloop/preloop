# AI Model Gateway

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

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
| `POST /anthropic/v1/messages` | Anthropic-format clients such as Claude Code and Claude Desktop |
| `POST /anthropic/v1/messages/count_tokens` | Token counting; forwarded upstream, not recorded or charged |
| `GET /anthropic/v1/models` | Model listing in Anthropic's shape, filtered by allowed models |
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
- **Validation is surfaced honestly.** Onboarding sends a direct gateway route/accounting probe using managed configuration; it does not launch the application. If the provider throttles it or refuses on billing/quota grounds, the agent stays enrolled but shows an **unverified** badge in the console (and a Reason in `preloop agents list`/onboarding summaries) until `preloop agents validate <agent> --live` passes. A successful probe verifies the direct route and accounting; application behavior remains unverified.

---

## Claude Desktop and the Claude apps gateway

Claude Desktop and Claude Code can reach Preloop's Anthropic surface in two ways.

**Desktop direct.** Set Desktop's `inferenceProvider` to `gateway`, `inferenceGatewayBaseUrl` to `https://<your-preloop>/anthropic`, and `inferenceGatewayApiKey` to a Preloop API key. Add `inferenceCustomHeaders: {"X-Preloop-Client": "claude-desktop"}` so usage rows record `client: claude_desktop`. Desktop's model picker reads `GET /anthropic/v1/models`. Attribution and budgets follow the API key, as for any other client.

**Claude apps gateway upstream.** A customer-run Claude apps gateway can use Preloop as its `provider: anthropic` upstream:

```yaml
upstreams:
  - provider: anthropic
    base_url: https://<your-preloop>/anthropic
    forward_user_identity: true
    auth:
      api_key: ${PRELOOP_UPSTREAM_KEY}
    headers:
      x-preloop-upstream-secret: ${PRELOOP_UPSTREAM_SECRET}
```

The key must be a **trusted upstream key**: an API key with the scope `model_gateway:trusted_upstream`. Only account admins can create one:

```bash
curl -X POST https://<your-preloop>/api/v1/auth/api-keys \
  -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
  -d '{"name": "claude apps gateway",
       "scopes": ["model_gateway:trusted_upstream"],
       "trusted_upstream_secret": "<at least 16 random characters>",
       "per_subject_budget": {"period": "monthly", "hard_limit_usd": 50}}'
```

On a trusted upstream key Preloop reads the identity headers the apps gateway adds with `forward_user_identity: true`:

| Header | Meaning |
|--------|---------|
| `x-claude-gateway-user-id` | IdP `sub`; required to identify the developer |
| `x-claude-gateway-user-email` | Developer email, when the IdP supplies one |
| `x-litellm-end-user-id` | Email fallback |

- Each developer becomes a **gateway subject**, keyed on the IdP `sub` and scoped to the key. When the email matches an existing member of the key's account, the subject is linked to that member, so the member's `user` budgets apply. Preloop never creates users, logins or permissions from these headers.
- On any other credential the identity headers are ignored, because any client can forge them.
- The secret is optional but recommended, because the key sits in the gateway's config file. When `trusted_upstream_secret` is set, a request without the matching `x-preloop-upstream-secret` header gets `401`. Preloop stores only a sha256 hash.
- Budgets: a `gateway_subject` budget policy limits one developer; `per_subject_budget` on the key is the default for every developer without their own policy; the key's `api_key` budget still caps the gateway as a whole. Subject-scoped `allowed_models` (governance subject type `gateway_subjects`, keyed by the subject id) apply to messages, `count_tokens` and `GET /anthropic/v1/models`. There is no REST writer for `gateway_subjects` yet; until there is, per-developer model limits are set in the account's subject governance store, and the key's own `allowed_models` covers every developer behind it.
- Usage rows carry `meta_data.gateway_source` (`claude_apps_gateway` or `direct`), `meta_data.client` (`claude_desktop`, `claude_code` or `unknown`), `gateway_subject_id` and `gateway_subject_email`. Audit entries name the subject.
- Sessions keep Claude Code's `x-claude-code-session-id`. A request without it is grouped as `gw:<gateway_subject_id>:<UTC date>`.

**Why denials are 429 on this path.** The apps gateway fails over to its next upstream on `403`, which would let a developer bypass a Preloop budget. It relays a `429` on an email-carrying request to the developer as-is. So on a trusted upstream key with identity headers, budget denials return:

```http
HTTP/1.1 429 Too Many Requests
retry-after: 86400
x-should-retry: false

{"type":"error","error":{"type":"billing_error","message":"Preloop budget exceeded for dev@example.com: ..."}}
```

`retry-after` is whole seconds until the budget period resets. Rate limits return `429` with `"type": "rate_limit_error"`. The account kill switch, `allowed_models` denials, model authorization denials (`model_not_authorized`) and content policy denials (`content_policy_denied`, including `require_approval` holds that were not approved) return `429` with `"type": "permission_error"` and `x-should-retry: false` on this path too, so failover cannot route around them. Requests without a trusted upstream identity get `429` for budget denials too, and keep `403` for policy denials (see [Status codes](#status-codes)). `POST /anthropic/v1/messages/count_tokens` is not budgeted, but it honours the kill switch: a halted account forwards nothing upstream. A developer whose IdP sends no email gets the same `429`, but the apps gateway treats it as a capacity error and fails over; that cannot be fixed on Preloop's side.

**Passthrough.** On the byte-faithful [subscription OAuth path](#subscription-oauth-passthrough), `anthropic-version`, `anthropic-beta` (any value) and every other `anthropic-*` request header are forwarded verbatim, as are unknown body fields and the `system` array. Models with a provider API key still go through the gateway's translation layer. Streams stay `text/event-stream`. Upstream error bodies and the `anthropic-ratelimit-unified-*` and `x-should-retry` response headers are relayed to the client.

---

## What Gets Recorded

Every gateway request can persist:

- token usage and estimated cost in `ApiUsage`
- provider, model alias, managed agent, runtime session, flow, and API key attribution
- normalized `model_gateway_call` events with redaction-aware payload capture (see [Redaction](../../security/redaction.md) for what is stored and how to store less)
- provider-neutral conversation previews for operator review

The console **Cost**, **Audit > Sessions** and **Models** views read from this ledger.

---

## Budget and Allowed Models

Before dispatching upstream, Preloop evaluates:

1. **Account-level** gateway budgets
2. **Flow-level** budgets when the call originates from a flow execution
3. **Subject-scoped** allowed-model lists on the active API key or managed agent

Soft limits can annotate responses; hard limits deny the call before it reaches the provider.

OSS includes account, flow, API-key and managed-agent budgets with soft and hard limits, plus spend summaries and budget-health alerts.

!!! cloud "Cloud and Enterprise"
    Per-user and per-team budgets, soft and hard limit notifications, and negotiated price overrides come from the RBAC and billing plugins. See [Enterprise Billing & FinOps](../integrations/enterprise-billing.md).

---

## Status codes

Every gateway router (`/openai/v1`, `/anthropic/v1`, `/gemini/v1beta`) uses one status per condition, with the body in the router's own provider format.

| Condition | Status | Anthropic `error.type` | OpenAI `error.type` / `error.code` | Gemini `error.status` | Headers |
|---|---|---|---|---|---|
| Budget hard limit (account, flow, user, team, API key, gateway subject, per-model, per-execution ceiling, trial or free hosted-model allowance, pricing required for enforcement) | `429` | `billing_error` | `insufficient_quota` / `insufficient_quota` | `RESOURCE_EXHAUSTED` | `retry-after: <seconds>`, `x-should-retry: false` |
| Rate limit (Preloop or upstream) | `429` | `rate_limit_error` | `rate_limit_error` / upstream value | `RESOURCE_EXHAUSTED` | `retry-after` when known; never `x-should-retry: false` |
| Policy denial (kill switch, model allowlist, model authorization, content policy) | `403` | `permission_error` | `permission_error` | `PERMISSION_DENIED` | none |
| Hosted credit exhausted (Cloud and Enterprise hosted billing) | `402` | unchanged | unchanged | unchanged | unchanged |

Details for a budget denial:

- `retry-after` is an integer number of seconds until the budget window resets, or `3600` when the reset is unknown.
- `x-should-retry: false` tells the Anthropic and OpenAI SDKs not to retry. Claude Code also stops retrying when `retry-after` is above 60 seconds.
- The OpenAI body keeps the Preloop machine code in `error.preloop_code` (`budget_limit_exceeded` or `execution_budget_exceeded`).
- Anthropic and Gemini messages keep the `Model gateway budget exceeded: ...` prefix (the per-execution ceiling says `Execution budget exceeded: ...`).
- The Gemini body adds `google.rpc.ErrorInfo` (`domain: preloop.ai`) and `google.rpc.RetryInfo` details. Gemini CLI treats a `retryDelay` above five minutes as a terminal quota error; a shorter one makes it wait for the window to reset before it retries.
- Usage and audit rows record status `429`, audit `error_type` `budget_limit_exceeded` and `error_class` `budget_exceeded`. Budget denials do not count toward the rate limit report.

Requests from a trusted upstream (the Claude apps gateway) that carry a developer identity also get `429` for policy denials, because the apps gateway fails over to its next upstream on `403`.

!!! warning "Migrating from `403` budget denials"
    Before this change a budget hard limit returned `403` (`permission_error`), the same status as a policy denial. Integrations that match `403` to detect an exhausted budget must match `429` with `billing_error` (Anthropic), `insufficient_quota` (OpenAI) or `RESOURCE_EXHAUSTED` (Gemini) instead. To tell a Preloop budget denial from an upstream rate limit, check `x-should-retry: false`, `error.preloop_code` on the OpenAI router, or the `Model gateway budget exceeded` message prefix.

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
