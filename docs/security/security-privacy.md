# Security & Privacy

Preloop is designed for organizations that need to trust their AI governance layer completely. This page explains what Preloop sees, stores, and how to deploy it securely.

## How the MCP Proxy Works

Preloop sits between your AI agent and your MCP servers:

```
AI Agent → Preloop Proxy → Your MCP Servers
```

When an agent calls a tool:

1. **Preloop receives the tool call** — the tool name, arguments, and metadata
2. **Policy evaluation** — Preloop checks access rules and approval workflows
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
| **Agent identity** | ✅ Read | API key identifies the agent |
| **Your prompts/conversations** | ❌ None | Preloop never sees your LLM conversations |
| **Your source code** | ❌ None | Only tool call arguments are visible |
| **Your MCP server credentials** | ✅ Stored (encrypted) | Required to connect to your MCP servers |

## What Preloop Stores

- **Tool call logs**: Tool name, arguments, result status, timestamps, and who approved/denied
- **Approval requests**: Pending, approved, and denied requests with approver comments
- **Audit trail**: Complete history of all tool calls and approvals for compliance
- **User accounts**: Email, hashed passwords, notification preferences
- **Configuration**: MCP server connections, access rules, approval workflows

### Data Retention

- Audit logs are retained indefinitely by default
- You can configure retention policies via the admin settings
- All data can be exported or deleted on request

### Regulatory Evidence Packs

Separate from the audit trail, four flow presets produce per-run evidence
packs from a CI-emitted SBOM: a versioned `result.json` plus human-readable
reports you retain. [Security audit presets (CRA evidence packs)](../guide/flows/security-audit-presets.md)
documents that contract, its verdicts, and its limits. The packs are
machine-generated evidence for a human assessor. They are not a conformity
assessment, not a certification, and not legal advice.

## Deployment Options

### Cloud Hosted (preloop.ai)

- Hosted on secure infrastructure
- Data encrypted at rest and in transit (TLS 1.3)
- SOC 2 compliance in progress
- Available for immediate use — sign up at [preloop.ai](https://preloop.ai)

### Self-Hosted (Open Source)

- **Full control**: Deploy on your own infrastructure
- **Your data stays yours**: tool calls, model traffic, and audit data never leave your network
- **Telemetry is limited and opt-out**: the only phone-home is an anonymous daily version check-in to preloop.ai (instance id, version, edition) that powers the update notice. Set `PRELOOP_DISABLE_TELEMETRY=true` to disable it entirely — opted-out installs never phone home. See [Upgrading Preloop](../operations/upgrade.md#disabling-telemetry-internaltest-installs).
- **Docker Compose** or **Kubernetes (Helm)** deployment

```bash
# Self-hosted deployment
curl -fsSL https://preloop.ai/install/oss | sh
```

See [Install the OSS Stack](../operations/installation.md).

### Enterprise Self-Hosted

- Everything in self-hosted, plus:
- RBAC, team management, quorum approvals
- CEL-based conditional policies
- Priority support and SLA
- Contact [sales@preloop.ai](mailto:sales@preloop.ai)

## Encryption

| Layer | Method |
|-------|--------|
| **In transit** | TLS (HTTPS) — terminate with the [installer's TLS overlay or your own reverse proxy](../operations/tls.md) on self-hosted deployments |
| **At rest** | Preloop Cloud: encrypted at rest. Self-hosted: use your platform's disk/volume encryption — the default Docker Compose stack does not encrypt the Postgres volume for you |
| **MCP server & provider credentials** | Stored via the secret service, encrypted with an application-level key derived from `SECRET_KEY` |
| **Passwords** | Bcrypt hashed |

## Network Architecture

Preloop needs outbound access to:

- **Your MCP servers** — to forward tool calls
- **Email provider** (SMTP) — for approval notifications
- **APNs / FCM** — for mobile push notifications (optional)
- **Slack / Mattermost** — for chat notifications (optional, webhook-based)

Preloop does **not** need access to:

- Your LLM provider (unless using the AI Model Gateway)
- Your source code repositories (unless using the GitHub App integration)
- Any third-party analytics service. The only outbound telemetry is the optional daily version check-in to preloop.ai, disabled with `PRELOOP_DISABLE_TELEMETRY=true`

## Responsible AI Integration

When using the AI Approval Workflows feature (Enterprise):

- AI models evaluate approval requests based on your custom criteria
- The AI model's decision and reasoning are logged in the audit trail
- AI approvals can be configured to require human confirmation for high-risk decisions
- You choose which AI model to use — your data is sent only to the provider you select

## Reporting Security Issues

If you discover a security vulnerability, please report it responsibly:

- Email: [security@preloop.ai](mailto:security@preloop.ai)
- We will acknowledge receipt within 24 hours
- We aim to provide a fix within 72 hours for critical issues
