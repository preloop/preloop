# Preloop OpenClaw Plugin

`@preloop/openclaw-plugin` exposes OpenClaw to Preloop without requiring the
Preloop CLI to be present at runtime.

The plugin reads its OpenClaw plugin entry config, keeps the Agent Control
WebSocket connected, advertises capabilities, routes operator prompts or
voice transcripts into OpenClaw sessions, and gates native tool calls through
Preloop mobile/watch approvals via the `before_tool_call` hook (fail-closed by
default). OpenClaw's own `~/.openclaw/exec-approvals.json` policy is honored
locally for auto-allow / auto-deny; only would-prompt cases escalate.

Set `tool_approval_enabled: false` on the plugin config to disable the gate, or
`tool_approval_fail_open: true` to allow tools when Preloop is unreachable.

## Install

```bash
openclaw plugins install @preloop/openclaw-plugin
```

OpenClaw runs the plugin installer inside its own Node runtime. If OpenClaw
reports `requires Node` or `Unsupported engine`, upgrade the Node executable used
by `openclaw`, then rerun `preloop agents install-plugin openclaw`.

For local development from this repository:

```bash
cd preloop/runtime-plugins/openclaw-preloop
npm install
npm run build
```

## Configure

The plugin does not require the Preloop CLI at runtime. Existing Preloop users
can still let the CLI provision `plugins.entries.openclaw-plugin.config`:

```bash
preloop agents onboard openclaw
preloop agents install-plugin openclaw
preloop agents validate openclaw
```

Restart OpenClaw after installation. When this plugin connects and advertises
capabilities, Preloop marks Agent Control verified and Talk appears for the
agent in web and mobile clients.

When installed from the OpenClaw marketplace or UI, the installer should prompt
for Preloop login or signup if that config block is missing. Because OpenClaw
scans extension bundles for credential-harvesting patterns, OAuth/token
bootstrap should live in the marketplace installer or a separate Preloop connect
helper, not in the runtime extension entrypoint. Users should not hand-author
`bearer_token` values.

The generated OpenClaw config contains:

```json
{
  "plugins": {
    "entries": {
      "openclaw-plugin": {
        "config": {
          "enabled": true,
          "protocol": "preloop.agent_control.v1",
          "runtime": "openclaw",
          "control_ws_url": "wss://staging.preloop.ai/api/v1/agents/control/ws",
          "bearer_token": "agt_...",
          "runtime_principal_id": "openclaw-...",
          "runtime_principal_name": "OpenClaw"
        }
      }
    }
  }
}
```

OpenClaw versions that validate config schemas reject unknown root keys, so do
not write Agent Control metadata as a top-level `preloop` object.

The standalone CLI verifier also accepts a direct control document:

```json
{
  "control": {
      "enabled": true,
      "protocol": "preloop.agent_control.v1",
      "runtime": "openclaw",
      "control_ws_url": "wss://staging.preloop.ai/api/v1/agents/control/ws",
      "bearer_token": "agt_...",
      "runtime_principal_id": "openclaw-...",
      "runtime_principal_name": "OpenClaw"
  }
}
```

## Manual Test Without Preloop CLI

```bash
openclaw plugins install @preloop/openclaw-plugin
preloop-openclaw-plugin verify --config ~/.openclaw/openclaw.json
preloop-openclaw-plugin run --config ~/.openclaw/openclaw.json
```

The marketplace installer or Preloop connect helper should open Preloop
login/signup and write the runtime control config. `verify` checks the config
shape and confirms the plugin can load. `run` opens the Agent Control WebSocket
and advertises capabilities. In Preloop, the agent should become
`control_online=true` and the web/mobile clients should show Talk.

To test a prompt manually, open the Preloop web UI, choose the OpenClaw agent,
click Talk, and send a short message. The plugin should forward the message to
OpenClaw and send a command result back to Preloop.

## Verify

```bash
preloop-openclaw-plugin verify --config ~/.openclaw/openclaw.json
```

## Publishing status

The package is intended for the OpenClaw plugin marketplace and npm as
`@preloop/openclaw-plugin`. Before publishing, run:

```bash
npm install
npm run build
npm pack --dry-run
```

See `../PUBLISHING.md` for the full npm/OpenClaw marketplace release checklist.
