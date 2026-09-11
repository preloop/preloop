# Real-Time Communication

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
