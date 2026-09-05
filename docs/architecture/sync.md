# Preloop Sync

Preloop Sync polls issue trackers, generates embeddings, and writes through `preloop.models`. This chapter covers the scheduler/worker, tracker clients, the sync data flow, and tracker scope rules.

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
