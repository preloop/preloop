# Security & Privacy

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This page explains what Preloop sees and stores, and how to deploy it securely.

## How the MCP Proxy Works

Preloop sits between your AI agent and your MCP servers:

```
AI Agent → Preloop Proxy → Your MCP Servers
```

When an agent calls a tool:

1. **Preloop receives the tool call**: the tool name, arguments, and metadata
2. **Policy evaluation**: Preloop checks access rules and approval workflows
3. **If allowed**, the call is forwarded to your actual MCP server
4. **If approval required**, the call is held until a human approves or denies
5. **If denied**, the agent receives an access denied response
6. **The result** from the MCP server passes back through Preloop to the agent

## What Preloop Sees

| Data | Access | Notes |
|------|--------|-------|
| **Tool names** | ✅ Read | Needed for policy evaluation |
| **Tool arguments** | ✅ Read | Needed for policy evaluation and audit |
| **Tool results** | ✅ Pass-through | Forwarded to the agent; optionally logged for audit |
| **Agent identity** | ✅ Read | API key or runtime credential identifies the agent |
| **Model prompts and completions, MCP-only usage** | ❌ None | If an agent only uses the MCP endpoint, its model traffic goes straight to the provider. Tool arguments and results are recorded with [field-name redaction](redaction.md#field-name-redaction) |
| **Model prompts and completions, through the model gateway** | ✅ Stored, redacted | Preloop sees the whole conversation and, while `MODEL_GATEWAY_CAPTURE_CONTENT` is `true` (the default), stores a [pattern-redacted](redaction.md#pattern-redaction) copy: previews up to `MODEL_GATEWAY_MAX_PREVIEW_CHARS` (default 32768) per message, request and response bodies up to `MODEL_GATEWAY_ACTIVITY_MAX_BODY_CHARS` (default 8192) per string. Set it to `false` to store no content |
| **Your source code** | ❌ None through the MCP endpoint | Only tool call arguments and results are visible. Flows clone repositories into their own execution workspace |
| **Your MCP server credentials** | ✅ Stored (encrypted) | Required to connect to your MCP servers |

## What Preloop Stores

- **Tool call logs**: Tool name, arguments (field-name redacted), result status, timestamps, and who approved or denied
- **Model gateway events**: model, tokens, cost, attribution and, by default, the redacted conversation content described above
- **Approval requests**: Pending, approved, and denied requests with approver comments
- **Audit trail**: Complete history of all tool calls and approvals for compliance
- **User accounts**: Email, hashed passwords, notification preferences
- **Configuration**: MCP server connections, access rules, approval workflows

### Data Retention

- Retention periods are set in the console under **Settings > Records**

### Regulatory Evidence Packs

Separate from the audit trail, four flow presets produce per-run evidence
packs from a CI-emitted SBOM: a versioned `result.json` plus human-readable
reports you retain. [Security audit presets (CRA evidence packs)](../guide/flows/security-audit-presets.md)
documents that contract, its verdicts, and its limits. The packs are
machine-generated evidence for a human assessor. They are not a conformity
assessment, not a certification, and not legal advice.

## Reducing what Preloop stores

| Setting | Effect |
| --- | --- |
| `MODEL_GATEWAY_CAPTURE_CONTENT=false` | Gateway events keep usage, cost and metadata but no message content |
| `MODEL_GATEWAY_MAX_PREVIEW_CHARS` | Shorter per-message previews (default 32768) |
| `MODEL_GATEWAY_ACTIVITY_MAX_BODY_CHARS` | Shorter stored request and response bodies (default 8192) |
| `MODEL_GATEWAY_AUTO_INDEX_INTERACTIONS=false` | Gateway interactions are not indexed for session search |
| Shorter retention under **Settings > Records** | Records are purged sooner |

What is and is not redacted, per surface, is on the [Redaction](redaction.md)
page. The OWASP page states the same posture under
[Sensitive Information Disclosure](owasp-top-10-coverage.md#sensitive-information-disclosure).

## Deployment Options

### Cloud Hosted (preloop.ai)

!!! cloud "Cloud"
    - Run by Preloop, encrypted in transit and at rest
    - Sign up at [preloop.ai](https://preloop.ai)

### Self-Hosted (Open Source)

- **Full control**: Deploy on your own infrastructure
- **Your data stays yours**: tool calls, model traffic, and audit data never leave your network
- **Telemetry is limited and opt-out**: the only phone-home is a pseudonymous daily version check-in to preloop.ai that powers the update notice. It sends a random instance id, the version, the edition and the instance metadata record. That record holds the result of the previous check-in and the time the install marker was delivered. The first successful check-in of a fresh install also carries a one-time `install_completed` marker with the installer's start and completion timestamps. Nothing else is sent. Set `PRELOOP_DISABLE_TELEMETRY=true` to disable it entirely: opted-out installs never phone home. See [Upgrading Preloop](../operations/upgrade.md#disabling-telemetry-internaltest-installs).
- **Docker Compose** or **Kubernetes (Helm)** deployment

```bash
# Self-hosted deployment
curl -fsSL https://preloop.ai/install/oss | sh
```

See [Install the OSS Stack](../operations/installation.md).

### Enterprise Self-Hosted

!!! cloud "Enterprise"
    Everything in self-hosted, plus users, roles, teams, multi-approver and
    quorum approvals, AI-driven approvals, and per-user and per-team budgets.
    Contact [sales@preloop.ai](mailto:sales@preloop.ai).

CEL conditions on access rules ship in OSS.

## Encryption

| Layer | Method |
|-------|--------|
| **In transit** | TLS (HTTPS): terminate with the [installer's TLS overlay or your own reverse proxy](../operations/tls.md) on self-hosted deployments |
| **At rest** | Preloop Cloud: encrypted at rest. Self-hosted: use your platform's disk/volume encryption, the default Docker Compose stack does not encrypt the Postgres volume for you |
| **MCP server & provider credentials** | Stored via the secret service, encrypted with an application-level key derived from `SECRET_KEY` |
| **Passwords** | Bcrypt hashed |

## Network Architecture

Preloop needs outbound access to:

- **Your MCP servers**: to forward tool calls
- **Email provider** (SMTP), for approval notifications
- **APNs / FCM**: for mobile push notifications (optional)
- **Slack / Mattermost**: for chat notifications (optional, webhook-based)

Preloop does **not** need access to:

- Your LLM provider (unless using the AI Model Gateway)
- Your source code repositories (unless using the GitHub App integration)
- Any third-party analytics service. The only outbound telemetry is the optional daily version check-in to preloop.ai, disabled with `PRELOOP_DISABLE_TELEMETRY=true`

## AI-Driven Approvals

!!! cloud "Cloud and Enterprise"
    - An AI model evaluates approval requests against your guidelines
    - The decision and reasoning are recorded with the approval
    - Below the workflow's confidence threshold, or when the model cannot
      decide, the configured fallback applies: escalate to a human, deny, or
      approve
    - You choose the model; the request goes only to that model's provider

## Reporting Security Issues

If you discover a security vulnerability, please report it responsibly:

- Email [security@preloop.ai](mailto:security@preloop.ai) with the details listed in
  [SECURITY.md](https://github.com/preloop/preloop/blob/main/SECURITY.md)
- We acknowledge receipt as soon as possible and agree disclosure timing with you
