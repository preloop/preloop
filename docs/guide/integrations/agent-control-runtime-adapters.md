# Agent Control Runtime Adapters

**Agent Control** is Preloop's live operator channel for enrolled agents such as OpenClaw and Hermes. Operators on web, mobile, or watch can send audited text (and voice-originated transcripts) to a running local agent. The agent must load a **runtime plugin** that owns the long-lived WebSocket to Preloop.

---

## Architecture

```text
Operator (console / mobile / watch)
        │
        ▼
Preloop API  POST /api/v1/agents/{id}/control/commands
        │
        ▼
Agent Control bus  (NATS when available, local fallback otherwise)
        │
        ▼
Runtime plugin  WS /api/v1/agents/control/ws
        │
        ▼
Native agent runtime  (OpenClaw / Hermes session)
```

MCP firewall and model gateway traffic are unchanged. Agent Control adds presence, operator messages, interrupts, and status — not a bypass around policy enforcement.

---

## The `preloop.control` Contract

Managed onboarding writes a configuration block (YAML for Hermes, JSON for OpenClaw) with fields such as:

| Field | Purpose |
|-------|---------|
| `control_ws_url` | WebSocket URL, typically `wss://…/api/v1/agents/control/ws` |
| `bearer_token` | Durable runtime credential for the enrolled agent |
| `runtime_principal_id` | Stable identity for presence and attribution |
| `managed_agent_id` | Link back to the console Agents view |
| `adapter_package` | Marketplace package name for the runtime plugin |

The CLI provisions this block during `preloop agents onboard`. Validation reports `control_config_written`, `control_plugin_installed`, `control_plugin_verified`, and `control_channel_configured` separately so MCP/gateway onboarding can succeed even when the plugin is not yet loaded.

---

## Standalone Runtime Plugins

Preloop ships open-source runtime plugins under `runtime-plugins/` in the main repository. They are **not** server-side EE plugins.

| Runtime | Package | Install path |
|---------|---------|--------------|
| OpenClaw | `@preloop-ai/openclaw-plugin` | `preloop agents install-plugin openclaw` or `openclaw plugins install @preloop-ai/openclaw-plugin` |
| Hermes | `preloop-hermes-plugin` | `preloop agents install-plugin hermes` or `pip install preloop-hermes-plugin` |

Each plugin:

- reads the CLI-written `preloop.control` configuration
- connects to the Agent Control WebSocket with reconnect/backoff
- advertises capabilities (text, voice transcript, interrupt, session modes)
- maps `send_message` commands into the native runtime
- emits command results and presence/heartbeat events back to Preloop

Hermes embeds the reusable Python client from `preloop.integrations.agent_control`. OpenClaw ships a standalone TypeScript plugin with the same wire protocol.

See the package READMEs and [PUBLISHING.md](https://github.com/preloop/preloop/tree/main/runtime-plugins/PUBLISHING.md) for marketplace release steps.

---

## Recommended Onboarding Flow

```bash
preloop agents onboard openclaw    # or hermes
preloop agents install-plugin openclaw
preloop agents validate openclaw
```

Restart the agent runtime after installing the plugin. When the plugin connects and sends presence, Talk controls in the console and mobile apps become available for that agent.

If native plugin installation fails (for example OpenClaw requires a newer Node version), the CLI can fall back to Preloop's managed Agent Control sidecar so validation can still confirm control readiness.

---

## Runtime Responsibilities

A loaded adapter must:

- keep the WebSocket open while the agent session is available for operator contact
- send heartbeat/status/presence envelopes so Preloop shows online state
- receive `send_message` commands from operator surfaces
- treat each operator message as an **audited user turn** in the active session — not a hidden system prompt or policy bypass
- route resulting tool calls and model calls through the configured MCP firewall and model gateway

Operator voice on mobile/watch uses native STT, then posts a normalized transcript through the same command surface.

---

## Protocol Summary

- **Wire protocol:** `preloop.agent_control.v1`
- **Runtime connect:** `WS /api/v1/agents/control/ws?token=…`
- **Operator send:** `POST /api/v1/agents/{agent_id}/control/commands`
- **Presence:** capability advertisement on connect; heartbeat/status envelopes while online
- **Commands:** `send_message` with text or `voice_transcript` input modes; optional interrupt flag

---

## Related

- [OpenClaw integration](openclaw.md)
- [Hermes reference](../hermes.md)
- [Mobile apps](../clients/mobile-apps.md)
- [Runtime Sessions](../concepts/runtime-sessions.md)
- [Architecture](../architecture.md)
