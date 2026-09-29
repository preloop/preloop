# What Preloop includes

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop has three surfaces: the web console, the API (MCP endpoint and model gateway), and the mobile and watch apps. Contributors looking for internals should read the [architecture chapters](../architecture/overview.md).

## Console

The **Console** is the web-based interface where you manage your organization, configure tools, monitor agent activity, and inspect model traffic. It provides a unified view of your issue trackers, automation flows, managed agents, runtime sessions, and governance state.

*   **Overview**: Metrics including active runtime sessions, recent tool-call volume, daily model spend, and system status.
*   **Cost**: Spend trends, attribution by model/agent/session, and budget-health signals (Enterprise adds budget-policy configuration behind feature flags).
*   **Audit > Sessions and optimization**: Per-session timelines with the session-optimization loop (analyze, one-click apply, replay-verify) in the **Optimize** tab.
*   **Trackers**: Connect issue trackers (Jira, GitHub, GitLab) that flows and tools work against.
*   **Flows**: Create and configure event-driven agentic workflows.
*   **Agents**: Inspect enrolled agents, recent sessions, captured gateway interactions, operator-driven session lifecycle actions, and live Agent Control presence/commands where the runtime adapter is connected.
*   **Models**: Configure reusable models, secret-backed credentials, gateway routing, and per-model usage visibility.
*   **Tools** and **Policies**: The tool catalogue, access rules and approval workflows.
*   **Settings**: Account, records retention, API keys, runners, webhooks, notifications and the emergency kill switch.

!!! cloud "Cloud and Enterprise"
    **Users**, **Teams** and **Invitations** appear in the console when user and team management are enabled.

## API

The **API** serves as the gateway for all interactions. It implements the **Model Context Protocol (MCP)** for tool use and the model-gateway endpoints for managed model traffic.

*   **MCP Server**: Securely exposes tools to AI agents.
*   **Model Gateway**: Provides OpenAI-compatible and Anthropic-compatible ingress so managed runtimes can send model traffic through Preloop instead of using direct provider credentials.
*   **Approval Engine**: Intercepts sensitive tool calls and routes them for human approval based on defined policies.
*   **Subject-Scoped Governance**: Evaluates account defaults together with managed-agent and API-key scope for tool visibility, tool rules, and allowed models.
*   **Authentication**: Handles user sessions, API tokens, and short-lived runtime credentials with runtime-principal attribution.
*   **Observability**: Records gateway usage, execution-scoped gateway events, runtime-session activity, and audit trails for operator review.
*   **Agent Control**: Provides the managed-agent WebSocket and operator command endpoint. The CLI provisions credentials/config and installs or verifies native runtime plugins via `preloop agents install-plugin`. Live agent behavior requires OpenClaw, Hermes, or another runtime to load the plugin that owns reconnect/backoff, heartbeat/status events, capability advertisement, command receipt, and message execution/injection into the active session.

## Mobile and watch apps

Stay connected and manage approvals on the go with the **Preloop** native applications.

These apps are **not open source**, but they can be used with **self-hosted/open-source** Preloop deployments.

*   **iPhone App**: Full access to your dashboard, issues, approval requests, and scaffolded Agent Control voice turns. Push notifications ensure you never miss a critical decision.
*   **Apple Watch App**: Quick approvals, status checks, and scaffolded dictation for short Agent Control messages right from your wrist.
*   **Android App**: Approval review, push notifications, and scaffolded Agent Control voice turns. Google Assistant/App Actions are invocation or deep-link handoff surfaces, not arbitrary background voice transports.

## For Contributors

The main repository ships a scripted end-to-end rig at
[`scripts/e2e-rig`](https://github.com/preloop/preloop/tree/main/scripts/e2e-rig):
it installs a pinned OSS release on a VM behind TLS, onboards every
discoverable agent with the CLI, exercises the gateway and optimization loop,
then offboards and asserts each agent's config was restored, recording
browser and terminal footage along the way. See its README for requirements
and usage; it always runs with `PRELOOP_DISABLE_TELEMETRY=true`.
