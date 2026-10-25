# Durable audit delivery and recovery

Audit collection, its query API and the console ship in every edition. By
default, asynchronous audit writes use a bounded best-effort worker. Set
`PRELOOP_AUDIT_OUTBOX_ENABLED=true` on every API/worker process and restart
those processes to enable durable delivery. Apply the database migrations
(`alembic upgrade head` from `backend/preloop/models`) before enabling it.
False or an absent flag keeps the best-effort path.

With the flag enabled, approval creation and decisions (including AI, bypass,
expiry and escalation), notification timelines and post-approval execution
results flush their audit intents before committing the business write. Policy
evaluators flush through the caller's session without committing it; the owners
of short-lived evaluation sessions commit before returning. Async sessions use
`run_sync` with their async driver; synchronous MCP outcome transactions run on
a worker thread. An audit admission failure rolls back the corresponding local
write. Approval and policy intents keep priority over tool-call traffic.

Dynamic MCP tool outcomes and their audit intents commit with the local runtime
activity row. Calls without a runtime session use a short standalone intent
transaction. Remote tool execution is outside the database transaction: a
rollback of the local outcome cannot undo an upstream tool's side effects.
Legacy MCP calls preserve the tool's result if standalone audit admission fails,
and count that failure for alerting. Notification fan-out finishes delivery before
recording each channel's timeline and intent in a separate transaction. Failed
channel admissions roll back together, are returned in `audit_errors`, and count
as failures without suppressing other channels or expiring the approval request.
Model-content policy hit workers commit the hit and its audit intent together;
notification delivery follows that commit. Redaction-only events have no hit
row and use their own short intent transaction. Work that has not committed
(including queued policy-hit work) is still in flight and cannot be replayed.

These transactional paths bypass the bounded audit executor, so filling its
ordinary and reserved slots cannot drop a committed business event. Other
`*_async` callers still use the bounded executor: work refused before its intent
commits is not durable. Keep admission-drop alerts for those callers and for
the default-off configuration. The flag does not capture activity outside
Preloop.

The relay starts with the API lifecycle, polls every second, and processes at
most 100 intents per pass. Delivery deletes the intent in the same transaction
that creates the audit row. Failed delivery retries with exponential backoff
(one second initially, capped at five minutes). After 20 unsuccessful attempts
the intent is parked as `failed`; unsupported operations park immediately.
Parked intents remain stored and are not retried automatically.

Monitor these OpenTelemetry instruments:

- `preloop.audit.outbox.oldest_pending_age` (seconds gauge): current age of the
  oldest due intent; zero after the due backlog drains. The existing
  `preloop.audit.outbox.lag_seconds` histogram remains available for trends.
- `preloop.audit.outbox.attempts_failed` (counter): failed deliveries, relay
  database/health-query failures, and standalone admissions after external tool
  or notification effects, with a bounded `reason` attribute. Failed admissions
  have no stored intent and cannot be replayed.
- `preloop.audit.outbox.parked` (gauge): retained failed intents requiring replay.
  It is read from the database on each relay pass and survives process restart.
  Parked intents and intents waiting for retry backoff are excluded from due lag.
- `preloop.audit.writes.dropped` (counter): remaining best-effort admission loss.

The Helm chart provides optional `PrometheusRule` resources with
`auditAlerts.enabled=true` for installations with Prometheus Operator and an
existing OTLP-to-Prometheus pipeline. This setting does not enable the outbox or
install a metrics pipeline. Set `auditAlerts.metricSelector` to match only this
installation, `auditAlerts.labels` to match your rule selector, and
`auditAlerts.metrics` to the names produced by your exporter (unit suffixes can
vary). Defaults alert on due lag above 60 seconds for five minutes, any failed
attempt or admission drop in five minutes, and parked intents for one minute.
Replica gauges observe the same database, so the rules use `max`, not `sum`.
The lag threshold and hold time are configurable. Confirm telemetry is being
exported before relying on these alerts.

Intent payloads are redacted before persistence, but still contain audit metadata
and should receive the same database access controls and backup policy as audit
records.

## Replay a bounded batch

Run the local operator command in the backend environment or API container,
using that deployment's `DATABASE_URL`. It requires direct deployment/database
access; it is not a tenant-facing API. First fix the delivery cause (for example,
a schema mismatch or database outage) and keep the outbox flag enabled so the
relay can consume requeued work.

Preview one account's oldest 100 parked intents without changing them:

```bash
python -m preloop.services.audit.replay \
  --account-id 11111111-1111-4111-8111-111111111111 --limit 100
```

The default is a dry run; `--dry-run` may also be passed explicitly. The output
is a count only and never prints stored payloads. The batch size must be 1–1000
(default 100), and an account ID is mandatory. After reviewing the count, apply one
batch explicitly:

```bash
python -m preloop.services.audit.replay \
  --account-id 11111111-1111-4111-8111-111111111111 --limit 100 --apply
```

Apply resets only the selected account's oldest failed intents to `pending`,
clears their prior failure and resets their attempt budget. Pending and already
delivered work are untouched. Concurrent operators skip one another's locked
rows, so the applied count can differ from an earlier preview. The command does
not wait for delivery or loop over further batches. Check delivery and failure
metrics before explicitly replaying another batch. An unfixed failure will park
again after its finite retry budget; do not schedule an automatic replay loop.

Turning the flag off stops the relay and routes subsequent asynchronous writes
back to best effort. It does not discard stored intents; enable the flag again
to resume pending delivery. Delivered intents are deleted, and replay does not
alter historical audit rows.
