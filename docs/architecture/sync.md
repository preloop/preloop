# Preloop Sync

Preloop Sync polls issue trackers, generates embeddings, and writes through `preloop.models`. This chapter covers the scheduler/worker, tracker clients, the sync data flow, and tracker scope rules.

Private webhook ingress prepares database changes on a worker thread and closes
the session before publishing any NATS tasks, including validation notifications
and unknown-project sync requests. Issue/comment transformations initialize no
network client; GitLab job and pipeline events require no tracker authentication
call. The existing `process_webhook_event` task generates embeddings before
triggering flows, so webhook responses never wait for the embedding provider.
Generation uses an owned session and snapshots provider settings before
committing and returning the connection during provider work. A progress heartbeat
renews the task's NATS lease during generation, including cancellation draining;
the worker acknowledges or requeues only after its active thread finishes.

An event still forwards when inline issue processing fails. NATS timeout and
connection-closed errors retry at most three times. Missing task acknowledgments
produce HTTP 503 with `Retry-After`, and the successful-delivery timestamp updates
only after acknowledgment. Tracker redelivery can repeat a previously committed
issue update or partially acknowledged publication; this path does not promise
exactly-once task delivery.

It does promise one execution per delivery. Message handling is at-least-once
(a drained pod naks its in-flight message, `ack_wait` expires, a pod can die
before acking), so the guarantee is durable rather than message-level: the
delivery id (`X-GitHub-Delivery`, `X-Gitlab-Event-UUID`) is recorded on the
execution it created as `flow_execution.webhook_delivery_key`, a partial
unique index on `(flow_id, webhook_delivery_key)` stops two workers racing the
same redelivery, and `FlowTriggerService.process_event` skips a flow whose
delivery already produced an execution (any status, 7 day window). Tracker
sources that send no delivery id fall back to a content fingerprint of the
event identity, which is only treated as a duplicate inside a 15 minute
redelivery window because such a fingerprint legitimately repeats. Retries,
matrix cells, manual and scheduled triggers never claim a key, and
`process_webhook_event` acks as soon as the trigger stage commits so a deploy
drain does not replay work that already happened. See
`preloop.services.webhook_delivery_dedupe`.

Deploy updated sync workers before the API when introducing the embedding-work
payload. Older workers accept extra event fields but do not execute the new
`embedding_requests` field; the worker-first rollout preserves embedding work
during mixed-version deployments.

## Preloop Sync ( `./backend/preloop/sync`)
*   **Purpose:** Data synchronization and embedding generation service.
*   **Functionality:**
    *   The `preloop.sync` CLI can launch one-off scan operations or start a persistent scheduler.
    *   **Scheduler:** Periodically adds polling tasks for each configured tracker to the NATS queue. The same daemon also reconciles native flow schedules (`sync/services/flow_schedules.py`): one APScheduler job per enabled, non-preset flow with `trigger_event_source='schedule'`. `flow.schedule_config` is a typed union — raw cron (`{"type": "cron", "expr": <5-field crontab>}`; the legacy `{"cron": ...}` shape is still accepted) or the friendly forms `interval` (`every`/`unit`, bounded between 5 minutes and 366 days), `daily` (`at: "HH:MM"`), and `weekly` (`days` + `at`), each with an optional IANA `timezone` (default UTC); the minimum 5-minute interval is enforced at the API for all forms. `POST /api/v1/flows/schedule/preview` validates a config without saving and returns its type, human description, and next run times. Each tick only publishes a `run_scheduled_flow` NATS task; the worker side (`FlowTriggerService.run_scheduled_tick`) re-checks state and enforces the policies — paused flows never fire, and overlapping ticks are skipped while a previous execution is still running (recorded as `flow_schedule_tick_skipped` audit events). Flow API responses expose the derived `schedule_state` (incl. next fire time).
    *   **Worker:** Consumes tasks from the NATS queue. Multiple, specialized worker groups can be deployed, each subscribing to a specific subset of tasks (e.g., polling, webhooks). This allows for independent scaling and monitoring of different task types.
*   **Execution:** Runs as two distinct, long-running processes (scheduler and worker) or as a one-off CLI command.


## Issue Tracker Clients (within Preloop Sync)
*   **Location:** Implementations reside within Preloop Sync.
*   **Structure:** Abstract base classes define common interfaces (`get_issue`, `create_issue`, etc.).
*   **Implementations:** Concrete classes for each supported tracker (Jira, GitHub, GitLab).
*   **Features:** Handles authentication, API specifics, rate limiting, and error mapping for each tracker.

## Tracker Scope Rules

For detailed rules on how Organizations and Projects limit scope during syncing and searching, see the [Tracker Scope documentation](https://docs.preloop.ai/admin/tracker-scope).

## Data Synchronization Flow (Preloop Sync)
1.  **Trigger:** `preloop.sync scan all` command is executed.
2.  **Preloop Sync Service:**
    *   Retrieves tracker configurations using `preloop.models`.
    *   For each configured tracker:
        *   Uses the appropriate Issue Tracker Client to poll the external API (e.g., Jira API) for new/updated issues since the last scan.
        *   Processes the fetched issues.
        *   Generates vector embeddings for new/updated issue text.
        *   Calls functions in `preloop.models` to insert or update issue data and embeddings in the database.
3.  **preloop.models:** Interacts with the PostgreSQL database to persist changes.
