# Event-driven employees

An employee is an enrolled managed agent targeted by a persistent Flow. Each
accepted event has a durable Flow execution and an independent bounded runtime
conversation. Codex uses the existing SDK sidecar; Nanobot provides the governed
DeepSeek-compatible harness. Configure an account-owned model with the Preloop
model gateway enabled. Employee tasks refuse to use a broad runtime credential
or an ungoverned provider connection.

## Configure an employee Flow

Enroll and start the selected runtime using its Agent Control integration.
Create a Flow with `agent_type: "codex"` (the persistent path selects the enrolled
agent's actual runtime), the gateway-enabled `ai_model_id`, and this configuration:

```json
{
  "agent_config": {
    "execution_path": "persistent",
    "target_agent_id": "ACCOUNT_OWNED_MANAGED_AGENT_UUID",
    "limits": {"max_turns": 10, "max_total_tokens": 32000, "max_usd": 2}
  },
  "timeout_seconds": 300,
  "trigger_event_source": "discord",
  "trigger_event_types": ["member_joined", "channel_message"],
  "trigger_config": {
    "employee_events": {
      "source": "discord",
      "connection_id": "ACCOUNT_OWNED_CONNECTION_UUID",
      "kinds": ["member_joined", "channel_message"],
      "subjects": ["guild:example:members", "guild:example:channel:help"]
    }
  },
  "webhook_config": {"employee_secret": "GENERATE_AT_LEAST_32_RANDOM_CHARACTERS"}
}
```

Generate the ingress secret locally with `python -c 'import secrets;
print(secrets.token_hex(32))'`. Keep it in the account's Flow configuration and the
bridge environment. The Flow write API accepts `employee_secret`; list/detail responses redact it, and updates that omit the field preserve the stored value. Explicit `null` clears it. It authenticates this integration; it is not a user login or
a permission grant. The service checks the owning account, managed-agent lifecycle,
Flow enablement, source, connection, event kind and subject on every delivery.
Payloads are limited to 64 KiB and bot/webhook messages are excluded.

For GitHub, GitLab and Jira use the existing tracker webhook setup. Set
`trigger_event_source` and `employee_events.connection_id` to the tracker UUID,
`employee_events.source` to the provider, and subscribe to `issue_created` or
`pull_request_merged` (`merge_request_merged` for GitLab). Subject scope can use a
trailing `:*`, such as `github:example/repository:issue:*`, to permit new issue IDs
inside one repository. Other wildcards are not supported. Ordinary tracker Flows
retain their existing behavior. Existing project and organization trigger filters
continue to apply before employee intake.

Additional trusted adapters can call
`preloop.services.employee_events.ingest_employee_event` after provider
verification, supplying account/source/connection identity independently of
message text. Chat assistant connections handle linked human commands separately;
the Discord bridge below supplies employee channel and membership events. The returned execution ID and `PENDING` status acknowledge durable
intake; they do not claim that the task completed. Read execution status, failure,
model usage, approvals and history through the existing Flow APIs/console.

## Discord Gateway bridge

Enable Server Members for joins and Message Content for explicitly selected
channels in the Discord application's privileged intents. Grant the bot access
only to the desired server/channels. Configure these environment variables in the
bridge's private service environment:

```sh
export DISCORD_BOT_TOKEN='SET_IN_PRIVATE_ENVIRONMENT'
export DISCORD_GUILD_ID='example'
export DISCORD_CHANNEL_IDS='help'
export PRELOOP_EMPLOYEE_EVENT_URL='https://preloop.example.com/api/v1/employee-events/FLOW_UUID'
export PRELOOP_EMPLOYEE_EVENT_SECRET='SAME_RANDOM_SECRET_AS_FLOW'
python -m preloop.integrations.discord_employee_bridge
```

The bridge reads joins and selected channel messages and sends no Discord replies.
It signs raw HTTP bodies with HMAC-SHA256. Stable Discord event identities dedupe
replays. A private checkpoint under `~/.preloop/discord-employee-*.json` records
Gateway session/sequence only after ingress acknowledgement; reconnects and process
restarts resume from that point. Discord can expire a Gateway session; reconnecting
with a fresh session cannot recover events outside Discord's available replay
window. Observe provider availability and execution status in operations.

## GlitchTip

Configure a GlitchTip/Sentry-compatible signed webhook to
`https://preloop.example.com/api/v1/employee-events/FLOW_UUID`. Set its signing
secret to the Flow's `employee_secret`; requests carry the hex HMAC-SHA256 of the
raw body in `X-Sentry-Hook-Signature`. Subscribe with `source: "glitchtip"`,
`kinds: ["error"]`, a connection identity chosen by the account owner, and subject
scope `project:PROJECT_ID:issue:*`. The accepted notification body includes
`data.project.id`, `data.event.event_id` (or `id`), and `data.event.issue_id` (or
`group_id`). If a GlitchTip version exposes a different notification envelope or
an unsigned webhook, translate it in an authenticated local relay to this shape;
do not expose an unauthenticated generic endpoint.

## Lifecycle, limits and recovery

PostgreSQL's existing delivery unique index arbitrates simultaneous submissions.
The event row is committed before dispatch. Existing NATS task dispatch, exclusive
claims, lease heartbeats, stale-lease recovery and bounded retry decisions run the
Flow. A failed publish leaves an observable pending execution for recovery.
Suspending an employee, disabling the Flow or using the account kill switch takes
effect on subsequent intake. Per-execution gateway credentials enforce model and
MCP scope, token/turn/USD limits and revocation; runtimes also enforce duration and
prompt/history bounds. The last gateway response may cross an already-spent
ceiling once, as documented by the existing Flow limit policy.

A role prompt and relevant workspace files provide the small current-state index.
Each independent event starts a new conversation; old task histories remain in the
execution/session timeline and are not concatenated into new prompts. Native
session results are mapped to the originating command. A stop addresses that
owned command/session, never whichever unrelated session happens to be current.

The Codex sidecar stores command receipts beside its control config by default
(`employee_state_path` can override). On restart, terminal deliveries replay their
recorded result without repeating effects. If it crashed during a command, it
reports an explicit failure requiring review before deliberate retry. Keep this
ledger with the sidecar identity and back it up with private runtime state. Do not
run multiple sidecars sharing one ledger concurrently. Runtime/gateway failures
remain visible in the Flow; unknown prior effects are not automatically retried.

Gateway API keys in command storage are encrypted using the existing server
Fernet key. Only authorized runtime delivery decrypts them. Activity/history and
result projections redact both plaintext and encrypted credential fields.

To roll back, disable the Flow, stop the bridge/runtime, then rotate the ingress
secret. Preserve execution records and the runtime replay ledger for audit and
idempotency. No database migration is required for employee execution itself.

## Local acceptance fixtures

All examples use synthetic identities and mocks; no external messages/model calls
are made. Set `PRELOOP_DISABLE_TELEMETRY=true` for every scripted run.

```sh
PRELOOP_DISABLE_TELEMETRY=true PYTHONPATH=backend pytest \
  backend/tests/services/test_employee_events.py \
  backend/tests/services/test_employee_event_reservation.py \
  backend/tests/services/test_control_credentials.py \
  backend/tests/endpoints/test_employee_event_ingress.py \
  backend/tests/agents/test_agent_control_executor.py
PRELOOP_DISABLE_TELEMETRY=true npm --prefix runtime-plugins/codex-preloop test
```

Against an isolated PostgreSQL database migrated to the current Alembic head,
`test_employee_events_db.py` runs intake/reservation/replay/tenant-denial fixtures
for both Codex and Nanobot employee identities. Existing
`test_flow_execution_worker_orchestration.py` and
`test_agent_control_persistence.py` exercise claims, stale recovery, durable command
acknowledgement and reconnect. Nanobot's runtime plugin tests exercise the actual
supported Nanobot SDK seam with a synthetic provider. These fixtures verify local
contracts; they do not certify a live provider's credentials or bot connectivity.

## Updating employee runtimes

Install the matching Preloop CLI and Codex sidecar together. For a development
CLI use the sanctioned `cd cli && make install-local`; deploy the sidecar built
from the same checkout using its normal plugin installation workflow. Do not copy
a binary over an existing CLI executable. Employee mode probes for the new
`permission-hook --require-flow-credential` capability and fails closed before
launching Codex when an older CLI is installed. This keeps an old hook from
falling back to a broad managed-agent credential. Testing/building the checkout
does not update an installed CLI or running sidecar.

Each Codex employee task gets a private `CODEX_HOME` with scoped provider/MCP
configuration and mandatory scoped native permission hooks. Credentials stay in
the subprocess environment, never in config files or command arguments. Curated
global AGENTS guidance, repository instructions and independent native hooks are
preserved, along with the explicit native approval policy and configured sandbox.
Broad auth/provider/MCP configuration and unrelated credential environment values
are not inherited. Task homes retain native session history in the private host
runtime temporary directory; do not delete one while its task runs. Subsequent
independent events start fresh sessions. The backend does not resume a prior task
from this temporary home after a host restart; it reports uncertain interrupted
work through the durable receipt, allowing an operator to review before retry.

The execution model URL and the API/MCP URL may use separate hosts. The
controller supplies `gateway.api_url` from `PRELOOP_URL`; set this to the API
origin used for runtime enrollment. The runtime verifies that API URL against
its enrolled control origin. Model calls use the supplied gateway URL, while
native permission and MCP calls use the verified API origin with the same scoped
execution token. Employee tokens also bind the owned managed agent, so suspending
that identity or revoking the token prevents further native, model and MCP calls.
