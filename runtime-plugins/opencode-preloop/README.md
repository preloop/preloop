# @preloop-ai/opencode-plugin

Preloop Agent Control plugin for [OpenCode](https://opencode.ai). It keeps the
Agent Control WebSocket connected from inside OpenCode's plugin host and
routes every tool-permission prompt OpenCode raises through Preloop's
approval system, so operators can approve or deny tool calls from the Preloop
console (web/mobile) while the agent keeps working.

The plugin mirrors the contract of the other Preloop runtime plugins
(`@preloop-ai/openclaw-plugin`, `@preloop-ai/claude-plugin`,
`preloop-hermes-plugin`). Its only runtime dependency is the `ws` package,
used to authenticate the control WebSocket with an `Authorization` header.

## How approvals flow

1. The plugin reads the `preloop.control` block that
   `preloop agents onboard OpenCode --approvals` writes into
   `~/.config/opencode/opencode.json` (the same command adds the package to
   the `plugin` array; OpenCode installs npm plugins with Bun on startup).
2. It connects to `preloop.control.control_ws_url` with the durable runtime
   bearer token (sent as `Authorization: Bearer` on the WebSocket upgrade via
   the `ws` package, so the token never appears in proxy/access-log query
   strings), advertises presence/capabilities (`runtime: "opencode"`,
   `tool_approval: true`), sends heartbeats, and reconnects with backoff.
3. **Every native tool call is gated in `tool.execute.before`**, regardless
   of OpenCode's own `permission` config. A user whose `opencode.json` sets
   `bash`, `edit`, `webfetch`, ... to `"allow"` never sees a local prompt and
   OpenCode never raises `permission.asked`, so this hook is the only place
   the call can be intercepted. The plugin maps the OpenCode tool id to the
   Preloop native tool vocabulary (`bash` -> `Bash`, `edit` -> `Edit`,
   `write` -> `Write`, `read` -> `Read`, `glob` -> `Glob`, `grep` -> `Grep`,
   `list` -> `List`, `webfetch` -> `WebFetch`, `task` -> `Task`; MCP tools
   pass through by name), POSTs `{source: "opencode", tool_name, tool_input,
   session_id, cwd}` to Preloop's permission-check endpoint, and awaits the
   decision. Account tool rules decide first; unmatched calls are escalated
   to a human approver. On deny (or a fail-closed timeout) the hook throws
   `Preloop denied <tool>: <reason>`, which OpenCode reports to the model as
   the tool error.
   - Read-only tools (`read`, `glob`, `grep`, `list`) go through only when the
     backend says so; add an allow rule for them in the Preloop console if you
     do not want to approve file reads.
   - The one local shortcut is `safe_read_auto_allow` (default `true`): a
     `bash` command that consists solely of read-only commands (`ls`, `cat`,
     `git status`, `git log`, `rg`, ... and plain `|` pipelines of them) is
     allowed without a round trip. This mirrors the Preloop CLI hook's Cursor
     default; set it to `false` to route those too.
   - Tools served by the Preloop MCP server (`preloop_*`) are skipped because
     Preloop already governs them server-side.
4. For users who keep OpenCode's `"ask"` permissions, the plugin also receives
   the `permission.asked` event through the plugin `event` hook and replies
   `"once"` (approved) or `"reject"` (denied) through the OpenCode SDK
   client. Decisions are deduped per tool call (session id + call id): a call
   the gate already decided is answered from that decision, so the operator
   is never asked twice for one call.
5. If no decision arrives within `approval_timeout_ms` (default 310 s) or the
   endpoint is unreachable, the configured fallback applies: fail closed
   (deny, the default) or fail open (approve) via
   `tool_approval_fail_open`. There is no local "ask" fallback for the gate:
   a timed-out approval denies the call.

Operator `send_message` commands arriving over the control WebSocket are
deduped by `message_id` (the backend replays undelivered commands on
reconnect) and delivered into the targeted OpenCode session through the SDK
`client.session.chat` surface.

## Remote control

The plugin also steers the local OpenCode agent from the Preloop console, so an
operator can drive it remotely like `@preloop-ai/openclaw-plugin`,
`@preloop-ai/claude-plugin` and `preloop-hermes-plugin`:

- **Send a turn** — a `send_message` command is forwarded as a user prompt into
  the targeted OpenCode session. The plugin prefers the async SDK
  `client.session.chat(...)` surface and falls back to the documented blocking
  [`client.session.prompt({ path, body })`](https://opencode.ai/docs/sdk/)
  (`parts: [{ type: "text", text }]`), bounded by `turn_timeout_ms` (default
  300 s) so a hung turn cannot stall the acknowledgement forever — the session
  itself keeps running.
- **Stop / interrupt** — a `stop` or `interrupt` command (or a `send_message`
  with `payload.interrupt: true`) maps to
  [`client.session.abort({ path })`](https://opencode.ai/docs/sdk/), which
  aborts the running session.
- **Session targeting** — the target session id comes from the envelope
  (`target_session_id`, `session_reference`, `runtime_session_id`) with a
  fallback to `preloop.control.session_reference`. OpenCode sessions are
  addressed by their native session id; the plugin steers existing sessions and
  does not create new ones (`new_session: false` in its presence payload).
- **Status events** — every command emits a `command_result` frame on success
  (which the backend accepts as the command ack) or `command_error` on
  failure, mirroring the other runtime plugins. Replayed `message_id`s are
  acknowledged as completed duplicates without re-executing.

Remote steering can be disabled entirely by setting
`remote_control_enabled: false`; presence then advertises `text: false` and
`interrupt: false`, and steering commands are rejected while tool approvals keep
working. Capabilities are advertised truthfully: `text: true` only when the SDK
client exposes `session.chat` or `session.prompt`, and `interrupt: true` only
when `session.abort` is available.

### A note on OpenCode's plugin API

OpenCode documents a typed `permission.ask` hook, but as of 2026 the
permission system never triggers it (see anomalyco/opencode issues #7006 and
#9229). The supported surface — used here — is the generic `event` hook with
`permission.asked` / `permission.replied` events plus an SDK reply, which is
the same integration path OpenCode's own ACP bridge uses.

## Install

The plugin is distributed via npm only (there is no OpenCode marketplace
listing). Add the package to `plugin` in your OpenCode config:

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

Written by `preloop agents onboard OpenCode --approvals` under
`preloop.control` in `~/.config/opencode/opencode.json` (the CLI keeps its
MCP server entry in the legacy `~/.config/opencode/config.json`; OpenCode
also reads `opencode.json`, which is where this block lives):

```json
{
  "preloop": {
    "control": {
      "runtime": "opencode",
      "protocol": "preloop.agent_control.v1",
      "control_ws_url": "wss://example.preloop.ai/api/v1/agents/control/ws",
      "bearer_token": "<durable runtime token>",
      "runtime_principal_id": "<principal id>",
      "permission_check_url": "<optional; overrides the derived permission-check endpoint>",
      "session_reference": "<optional default session>",
      "tool_approval_enabled": true,
      "native_tool_approvals": "on",
      "safe_read_auto_allow": true,
      "tool_approval_fail_open": false,
      "approval_timeout_ms": 310000,
      "remote_control_enabled": true,
      "turn_timeout_ms": 300000
    }
  }
}
```

Set `PRELOOP_OPENCODE_CONTROL_CONFIG` to point at an alternative config file.

`native_tool_approvals` gates the `tool.execute.before` interception: `"off"`
disables it (the `permission.asked` bridge keeps working); unset or any other
value enables it. `preloop agents onboard OpenCode --approvals` writes `"on"`
and `preloop agents offboard OpenCode` removes the block and the `plugin`
entry. `safe_read_auto_allow` (default `true`) lets read-only shell commands
run without a round trip.

`permission_check_url` is optional: when unset, the plugin derives
`<origin>/api/v1/agents/permission-check` from `control_ws_url`. Set it when a
proxy or non-standard deployment serves the permission-check endpoint at a
different URL.

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
