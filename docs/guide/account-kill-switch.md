# Account kill switch

Use **Settings > Account > Emergency Controls** to halt an account. Owners,
admins and roles granted `manage_kill_switch` can activate or recover scopes.
Every authenticated account member can see the persistent console banner and
read `GET /api/v1/account/kill-switch/status`.

Activation accepts `POST /api/v1/account/kill-switch/activate` with a `reason`
and optional `scopes`. Omit scopes for a full halt:

- `gateway` rejects new OpenAI, Anthropic and Gemini gateway requests with HTTP
  403, `preloop_account_halted`, and the `kill_switch` error class. Rejections
  remain attributable in usage records.
- `tools` denies MCP dispatch, including approved calls awaiting replay. Pending
  approvals retain their remaining timeout. Recovery adds exactly the time each
  pending request spent frozen, even if no worker polled during the incident.
  Requests already expired before activation are not revived.
- `flows` blocks new launch admission and durably requests termination of managed
  executions that were already admitted. Waiting executions without a runtime
  remain pending until recovery. A launch admitted immediately before activation
  may finish starting, but its durable stop request remains in force.

State changes, actor/time/reason audit records, approval deadline recovery and
managed stop requests commit in one transaction. Repeated requests are audited
with `changed: false`; they preserve the original activation attribution and do
not extend approval deadlines twice.

## Propagation and confirmation

Gateway and ordinary tool dispatch use a bounded cache with a five-second TTL.
The serving process invalidates on successful transitions; other processes
converge within five seconds under normal database availability. An invalidated
in-flight lookup cannot restore stale cache state. Repeated invalidation or
unavailable halt state denies requests instead of silently allowing them.
Post-approval dispatch checks fresh state.

Execution monitors read durable stop requests on their five-second poll and call
runtime stop. This is the observation interval, not a guarantee that a runtime
will finish terminating in five seconds. Docker allows its existing shutdown
grace period; Kubernetes waits for foreground removal of the Job and Pods;
private runners must receive and acknowledge the stop. Offline runners and
unreachable runtime APIs cannot provide immediate confirmation.

The execution detail API exposes `stop_requested_at`, `stop_source`,
`stop_reason` and `stop_confirmed_at`. A requested timestamp without a confirmed
timestamp means termination remains outstanding, even when the account has
already been re-enabled or a monitor reported an error. Recovery workers continue
attempting unconfirmed stops. A runner `halt_requested` flag alone never confirms
termination. Confirmation requires authoritative runtime evidence, the owning
runner's completion report, or atomic cancellation before runner assignment.

## Staged recovery

Record a recovery reason and restore scopes individually using the console or
`POST /api/v1/account/kill-switch/deactivate`, for example
`{"scopes":["gateway"],"reason":"Provider and policy checks verified"}`.
Restore gateway first, verify behavior, then tools, then flows. Inspect outstanding
execution stop requests before resuming automation. Lifting the flows halt does
not revoke stop requests or restart terminated executions; start a new execution
when ready. Each recovery action records its actor, time and reason.

The halt does not undo external side effects that already happened. Independent
processes and requests that bypass Preloop cannot be universally revoked by this
control. Runtime API failure or a disconnected private runner remains visible as
an unconfirmed stop rather than a claim that all activity ended.
