# Real-Time Communication

Editions: OSS. Contributor documentation for this repository.

Preloop uses a single WebSocket per client with pub/sub routing. This chapter covers MessageRouter, topics, and the unified realtime architecture.

Preloop uses WebSocket connections for real-time updates:

## Unified WebSocket Architecture

Single WebSocket connection per client with pub/sub message routing:

**MessageRouter** (`backend/preloop/services/message_router.py`):
- Routes messages to topic-based subscribers
- Supports wildcard subscriptions (`'*'` topic)
- Optional per-subscriber filter functions
- Topics: `flow_executions`, `approvals`, `system`

**Benefits:**
- Single WebSocket reduces connection overhead
- Scalable pub/sub pattern
- Easy to add new message types/topics
- Clear separation of concerns

> **Enterprise Features**: Preloop Cloud and Preloop Enterprise add RBAC and approval workflows with quorum, escalations and AI gates. Contact sales@preloop.ai for more information.

## Execution log persistence

Account events and gateway execution events are encoded before submission to a
process-local realtime publisher. API, scheduler and worker startup attach its
single drain task to the event loop that owns the shared NATS client. Calls from
worker threads or other event loops enqueue bytes without waiting for NATS, so
gateway bookkeeping cannot hold a database connection during a forced flush on
the wrong event loop.

The publisher bounds pending work by both message count and payload bytes,
including in-flight messages and thread hand-offs. Each publish has a timeout;
shutdown drains within a deadline and rejects new work. Rejected messages and
publish failures are counted and logged. This is best-effort live delivery:
overflow, timeout or shutdown can lose a notification, while persisted activity
remains available through history reads. It does not replace durable task
publication or its JetStream acknowledgement.

Each API process receives realtime flow updates through Core NATS. A separate
`log-persisters` queue subscription assigns each log to one process for database
persistence. Each process has one writer with a bounded queue of 10,000 entries
and one active batch of at most 500 entries. Producers await queue capacity;
these bounds count entries, not payload bytes. A short coalescing window gathers
logs arriving on separate event-loop ticks into one database transaction.

The pinned nats-py client dispatches messages into each subscription's bounded
pending queue without awaiting that subscription's callback. One worker per
subscription invokes its callbacks serially. Waiting for log queue capacity
therefore pauses the `log-persisters` worker while the separate flow, account,
approval and admin realtime subscriptions continue processing. No per-message
producer tasks are created. If the persister's NATS pending buffer also fills,
the NATS client reports a slow-consumer error and drops that subscription's overflowing
messages; it cannot provide durable backpressure to publishers. This transport
limit is separate from retention of logs already accepted into the writer queue.

Database sessions exist only during a write attempt. The CRUD layer scrubs each
batch and inserts it with stable row IDs, so retrying after an ambiguous commit
response cannot duplicate the same accepted event. Pool checkout timeouts and
connection failures trigger bounded synchronous retry cycles followed by an
asynchronous delay. The writer retains the batch until the database recovers;
exhausting one retry cycle does not drop it. Known logs for deleted executions
are discarded, while non-retryable data errors still produce a loss alert.

Shutdown stops subscriptions and allows accepted logs to drain. Cancellation
joins an active database write and retains unfinished batches for a writer
restart on the same event loop. A drain timeout reports both queued and active
counts. The queue and retained batch are process memory: a process exit, an
extended outage that exceeds Core NATS client buffering, or a failed publish can
still lose logs. Core NATS provides no durable acknowledgement or redelivery.
Restart-safe delivery requires a separate durable transport change. Limiting
writer concurrency reduces pool pressure but does not reserve capacity away
from API requests.

## Execution snapshots and sequence domains

`/api/v1/ws/flow-executions/{execution_id}` authenticates the owning account,
subscribes to Core NATS, and flushes the subscription before reading the database.
It sends `connected`, then `snapshot`, then live events. The send gate prevents an
event received during the database read from overtaking the snapshot. Snapshot
fields are `execution_id`, `flow_id`, `status`, `agent_session_reference`,
`start_time`, `end_time`, `stop_requested_at`, `stop_confirmed_at`, `seq`,
`lifecycle_seq`, and `sequence_domain`. Datetimes are ISO strings; absent values
are null. Lifecycle events at or below the snapshot watermark are superseded by
the authoritative state. Activity payloads are not contained in that snapshot:
queued log, gateway and tool events still pass through after the snapshot.

Every orchestrator, runner and model-gateway execution update allocates `seq`
through an atomic PostgreSQL upsert in the CRUD layer. The independent
`execution_realtime_cursor` row avoids contention with business writes on the
execution row. `seq` is an **allocation order**, not database state version,
delivery order, durable acknowledgement or proof of authenticated completion.
Publishers can deliver out of order; failures after allocation leave holes.
Cursors survive worker restarts and are scoped to an execution, never an account.
Deploy migration `20261120_execution_realtime_seq` before the new publishers.
During a rolling deployment, old publishers can still emit unsequenced events.
If allocation is unavailable, publishers preserve best-effort delivery with an
explicit unsequenced `resync_required` marker instead of fabricating a cursor or
dropping a log notification. The console coalesces recovery and admits at most
one recovery request group per two seconds, including ongoing gap bursts.

For recovery, GET `/api/v1/flow-executions/{execution_id}/lifecycle` reads state
and allocation watermarks in one authorized database statement. Keep the largest
seen sequence and coalesce a refresh after a jump greater than one. Suppress
older lifecycle state, but do not discard unseen activity solely because its
sequence is below a snapshot watermark. The console tracks the most recent
4,096 activity sequences for duplicate suppression and merges retained logs,
bounded gateway metadata and tool activity during recovery. Overlapping log
pages are deduplicated while their pagination offset advances by stored rows
read. Discard a late snapshot whose cursor is below the local cursor.
The console refreshes on initial subscription and reconnection as well. Core
NATS provides no durable replay; a missed final event with no later sequence
cannot itself announce a gap. Consumers needing eventual terminal state must
also perform a bounded authoritative status check after their idle deadline.
A socket event does not substitute for the separately authenticated terminal
webhook or validated result read.

### Bounded flow lifecycle startup

For clients with a small parser limit, use
`/api/v1/ws/flows/{flow_id}/lifecycle`. This endpoint authorizes `view_flows` and
account ownership before subscription, and rechecks permission before sending
lifecycle data. It does not register for unified account broadcasts. It filters
both account and flow before projecting an allowlist of lifecycle scalars. Logs,
results, prompts, tool arguments and fleet context never enter an outgoing
frame. Every outgoing lifecycle frame is at most 8,192 UTF-8 bytes; an unusually
large scalar projection becomes an explicit `resync_required` invalidation.

Wait for `connected` with `stream: lifecycle`, then trigger that flow. No trigger
or inference occurs merely by connecting. Send text `ping` at least every 30
seconds to keep the idle connection alive. The startup stream has no historical
execution enumeration; use the execution returned by the trigger for subsequent
snapshot reads. Account/flow IDs missing from a publisher event fail closed.

This filtered stream exposes `lifecycle_seq` as `seq`, with
`sequence_domain: lifecycle`. Only lifecycle event allocation advances that
counter. Intentionally suppressed log/tool/model events therefore create no
false gaps. Recover using the same GET endpoint with
`?sequence_domain=lifecycle`; never compare cursors from different domains.

The general `/ws/unified` remains useful for the console: authenticate, send
`{"type":"subscribe","topic":"flow_executions"}`, wait for its subscription
acknowledgement, then trigger. That stream is account-wide and has no small-frame
guarantee. Client-side filtering cannot prevent an oversized frame from reaching
the parser; bounded automation clients should use the flow lifecycle endpoint.
