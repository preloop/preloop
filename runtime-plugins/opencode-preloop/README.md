# @preloop-ai/opencode-plugin

Preloop Agent Control plugin for [OpenCode](https://opencode.ai). It keeps the
Agent Control WebSocket connected from inside OpenCode's plugin host and
routes every tool-permission prompt OpenCode raises through Preloop's
approval system, so operators can approve or deny tool calls from the Preloop
console (web/mobile) while the agent keeps working.

The plugin is standalone: it has zero runtime dependencies and mirrors the
contract of the other Preloop runtime plugins (`@preloop-ai/openclaw-plugin`,
`@preloop-ai/claude-plugin`, `preloop-hermes-plugin`).

## How approvals flow

1. The plugin reads the `preloop.control` block that
   `preloop agents enroll` writes into `~/.config/opencode/opencode.json`.
2. It connects to `preloop.control.control_ws_url` with the durable runtime
   bearer token, advertises presence/capabilities (`runtime: "opencode"`,
   `tool_approval: true`), sends heartbeats, and reconnects with backoff.
3. When OpenCode's permission system raises a prompt, the plugin receives the
   `permission.asked` event through the plugin `event` hook, dedupes it by
   request id, and POSTs it to Preloop's permission-check endpoint (tool
   name, proposed patterns, session id).
4. The backend parks that request until an operator decides. The decision is
   applied by replying `"once"` (approved) or `"reject"` (denied) through the
   OpenCode SDK client — OpenCode holds tool execution open until a reply,
   so awaiting the operator genuinely blocks the tool call.
5. If no decision arrives within `approval_timeout_ms` (default 310 s) or the
   endpoint is unreachable, the configured fallback applies: fail closed
   (reject, the default) or fail open (approve) via
   `tool_approval_fail_open`.

Operator `send_message` commands arriving over the control WebSocket are
deduped by `message_id` (the backend replays undelivered commands on
reconnect) and delivered into the targeted OpenCode session through the SDK
`client.session.chat` surface.

### A note on OpenCode's plugin API

OpenCode documents a typed `permission.ask` hook, but as of 2026 the
permission system never triggers it (see anomalyco/opencode issues #7006 and
#9229). The supported surface — used here — is the generic `event` hook with
`permission.asked` / `permission.replied` events plus an SDK reply, which is
the same integration path OpenCode's own ACP bridge uses.

## Install

Add the package to `plugin` in your OpenCode config:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@preloop-ai/opencode-plugin"]
}
```

Or drop a shim into `.opencode/plugins/preloop.ts`:

```ts
export { PreloopPlugin as default } from "@preloop-ai/opencode-plugin";
```

## Configuration

Written by `preloop agents enroll` under `preloop.control` in
`~/.config/opencode/opencode.json`:

```json
{
  "preloop": {
    "control": {
      "runtime": "opencode",
      "protocol": "preloop.agent_control.v1",
      "control_ws_url": "wss://example.preloop.ai/api/v1/agents/control/ws",
      "bearer_token": "<durable runtime token>",
      "runtime_principal_id": "<principal id>",
      "session_reference": "<optional default session>",
      "tool_approval_enabled": true,
      "tool_approval_fail_open": false,
      "approval_timeout_ms": 310000
    }
  }
}
```

Set `PRELOOP_OPENCODE_CONTROL_CONFIG` to point at an alternative config file.

## Manual Test Without Preloop CLI

```bash
npm install -g @preloop-ai/opencode-plugin
# add {"plugin": ["@preloop-ai/opencode-plugin"]} to ~/.config/opencode/opencode.json
preloop-opencode-plugin verify --config ~/.config/opencode/opencode.json
preloop-opencode-plugin run --config ~/.config/opencode/opencode.json
```

Then start `opencode`, trigger a permission-gated action, and approve/deny
it from the Preloop console.

## Development

```bash
npm install
npm run build   # tsc -> dist/
npm test        # builds, then runs node --test against dist/
npx tsc --noEmit
```
