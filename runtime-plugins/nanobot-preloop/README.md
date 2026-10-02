# Nanobot with Preloop

This standalone process embeds **nanobot-ai 0.1.4.post3** through its Python
`AgentLoop.process_direct`, `CustomProvider`, and `ToolRegistry.execute` seams.
It does not claim a native Nanobot plugin API or modify an existing channel bot.
The pinned version is intentional: newer upstream versions changed the loop API.
Upstream reference: https://github.com/HKUDS/nanobot/tree/v0.1.4.post3/nanobot.

Install into a dedicated virtual environment from the repository:

```sh
python3 -m venv ~/.local/share/preloop-nanobot/venv
~/.local/share/preloop-nanobot/venv/bin/pip install ./runtime-plugins/nanobot-preloop
```

Enroll using an authenticated **user** access token obtained through your
instance's supported login flow. Set `PRELOOP_ACCESS_TOKEN` through your secret
manager or shell environment; do not put the token on a command line.

```sh
preloop-nanobot-plugin enroll --base-url https://app.preloop.ai
unset PRELOOP_ACCESS_TOKEN
preloop-nanobot-plugin verify
preloop-nanobot-plugin run
```

The command creates `~/.nanobot/preloop.json` with mode 0600 and a separately
issued runtime credential. `NANOBOT_HOME` or `--config` changes discovery.
Enrollment refuses to overwrite an existing file. Verification checks local
configuration and SDK version; it does not claim remote connectivity or model
entitlement. Run as a managed foreground service with its virtualenv executable.
The Agent Control connection announces presence, capabilities, completion,
errors and replies through the existing Preloop protocol. Online presence and a
successful authorized model run are separate checks.

Model requests use the Preloop `/openai/v1` gateway. The default is
`deepseek-chat`; change the top-level `model` to an authorized gateway alias for
another provider. Provider selection and credentials stay with Preloop. Configure
account/agent spending policy in the gateway. The only MCP server is the same
instance's `/mcp/v1`, using the runtime credential. Arbitrary stdio or external
MCP configuration is deliberately excluded.

Every native and dynamically loaded MCP execution requires an explicit
well-formed allow from `/api/v1/agents/permission-check`. Denial, timeout,
malformed decisions and credential/network errors prevent execution. Native
`spawn`, `cron` and `message` are disabled because their background/outbound
lifecycles do not implement this owned task contract. Shell execution remains a
native tool gated by Preloop policy; configure appropriately scoped shell rules.

Tasks use owned UUID session references persisted in `preloop-sessions.json`.
A different runtime principal cannot adopt that store. Use `session_mode=new`
for unrelated tasks and `existing` with the returned session reference to resume.
Cancellation accepts an owned reference or the original `target_command_id`
while that command is active. Independent turns run serially because Nanobot's
registry and context are shared. History survives restart; automatic background
memory consolidation is cancelled when a turn ends.

`metadata.run_limits` supports `max_turns` (default 20, ceiling 100),
`timeout_seconds` (default 300, ceiling 3600), and `max_total_tokens` (default
100000, ceiling 1000000). Recent history, input and outgoing model context also
have fixed size ceilings. Flow dispatch supplies `metadata.gateway` with an execution-scoped credential,
model and instance gateway URL. Both model and MCP connections use this
credential for that turn; it never replaces the enrolled credential. Split-service
deployments can supply a separate HTTPS model gateway in `gateway.base_url`
when `gateway.api_url` explicitly matches the enrolled API origin. MCP and native
permission checks stay on that API origin; model traffic alone uses the dedicated
gateway.
`max_duration_seconds` aliases the timeout; `max_history_chars` can lower the
history ceiling. Monetary run limits (`max_usd`) require this execution-scoped
gateway, whose existing Flow policy enforces spending. Interactive messages
without it use the enrolled credential and account/agent gateway budget. No voice, desktop or background-agent support
is advertised. On timeout or cancellation, subprocess side effects already
performed cannot be rolled back.

For rollback, stop the service, revoke its runtime credential in Preloop, and
remove the dedicated environment. Preserve the configuration and session files
if conversation history is needed. The integration does not rewrite native
Nanobot configuration or session databases.

Run local tests without contacting a model or sending channel messages:

```sh
PRELOOP_DISABLE_TELEMETRY=true \
PYTHONPATH=backend:runtime-plugins/nanobot-preloop/src \
pytest runtime-plugins/nanobot-preloop/tests
```

The offline fixture exercises a real pinned SDK provider, session creation and
resume without HTTP or channel delivery:

```sh
PRELOOP_DISABLE_TELEMETRY=true \
PYTHONPATH=backend:runtime-plugins/nanobot-preloop/src \
python runtime-plugins/nanobot-preloop/tests/sdk_fixture.py
```

Command receipts are stored beside session ownership in a separate mode-0600
ledger. A file lock and fsynced atomic replacement persist intent before execution.
Duplicate running commands are acknowledged without starting another turn;
completed commands replay completion status and their native session reference.
Reply bodies, prompts, operator metadata and credentials are excluded from this
ledger, so replay does not resend the original reply body. Reusing a command ID
with different input is rejected. Pending receipts after a crash, and failed or
cancelled turns whose effects may already have occurred, return `outcome_unknown`
and never execute again automatically. Receipts are retained; reaching the
configured hard capacity of 100000 commands refuses new work rather than evicting
replay protection. Preserve this ledger when retaining runtime history.

Command duration includes time waiting for another turn. An ISO timestamp with
an explicit timezone in `metadata.expires_at` (or `run_limits.expires_at`) can
shorten that deadline; expired work never starts.
