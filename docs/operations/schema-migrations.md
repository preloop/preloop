# Schema migrations during an upgrade

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

`helm upgrade` runs schema migrations from a `pre-upgrade` hook Job
(`preloop-migration-job`). That Job starts **before** the new pods roll out and
**while the previous API pods, sync workers and connected private runners keep
serving**. A migration is therefore a concurrent writer competing for locks with
live traffic, not a maintenance window.

This page describes what the hook does about that, and the one case where an
operator still has to act.

## What the hook does

The Job runs `python -m preloop.models.migrate`, not bare
`alembic upgrade head`. It separates the following budgets.

**One transaction per revision.** Alembic can apply a whole batch of pending
revisions inside a single transaction. It then holds every lock it has taken
until the last revision commits, so an `ALTER TABLE` early in the batch keeps
its ACCESS EXCLUSIVE lock on that table while later revisions run. With
`transaction_per_migration` each revision commits on its own and releases its
locks immediately, and a failed run resumes from the last revision that
committed.

**A short session `lock_timeout`** (`migrationJob.lockTimeout`, default `5s`).
This is set on the migration's connection only. It is deliberately much shorter
than the cluster-wide `database.cnpg.resilience.lock_timeout`, which stays as
configured: a pending ACCESS EXCLUSIVE request also blocks every query that
queues behind it, so a migration that waits a minute for a lock stalls reads of
that table for a minute.

**A separate budget for concurrent indexes.** Explicit
`concurrent_index_ddl` blocks allow 60 seconds to acquire a lock and 120 seconds
for the entire statement. Concurrent index operations wait for older writers
and snapshots while ordinary reads and writes can continue. Canceling every
five seconds can leave an invalid index and repeatedly restart its repair.
The longer budget applies only to these explicit blocks, including invalid-index
cleanup; the previous lock and statement settings are restored on success or
failure. Blocking schema changes keep the five-second lock timeout. New
migrations that build or drop concurrent indexes must use this context in
both upgrade and downgrade; keep ordinary table DDL outside that scope.

**A bounded retry loop** (`migrationJob.maxAttempts`, default 20, with
`migrationJob.retryMinSeconds` to `migrationJob.retryMaxSeconds` of jittered
backoff). Only lock contention is retried: deadlocks (`40P01`), lock timeouts
(`55P03`) and serialization failures (`40001`). A revision with a bug fails on
the first attempt with its own error, because retrying it twenty times would
only delay the report. A statement timeout (`57014`) also ends the container
attempt. For blocking DDL, investigate whether a drain is needed. For concurrent
indexes, inspect older transactions and index progress before tuning only the
concurrent budgets; the deployment deadline is unchanged.

The shorter blocking-DDL budget limits disruption to serving traffic. The
separate concurrent-index budget allows safe waiting without repeatedly
canceling a build that is making progress.

## Defaults

| Value | Default | What it controls |
| --- | --- | --- |
| `migrationJob.lockTimeout` | `5s` | How long one revision waits for a lock before giving up. |
| `migrationJob.concurrentLockTimeoutSeconds` | `60` | Lock wait only inside concurrent-index blocks; positive integer, maximum 300. |
| `migrationJob.concurrentStatementTimeoutSeconds` | `120` | Whole concurrent statement budget; positive integer, maximum 240. |
| `migrationJob.maxAttempts` | `20` | Attempts at `upgrade head` before the Job fails. |
| `migrationJob.retryMinSeconds` | `3` | Lower bound of the jittered backoff. |
| `migrationJob.retryMaxSeconds` | `15` | Upper bound of the jittered backoff. |
| `migrationJob.backoffLimit` | `2` | Pod-level restarts, for a crashed container. |

The same settings are read from the environment
(`PRELOOP_MIGRATION_LOCK_TIMEOUT`, `PRELOOP_MIGRATION_MAX_ATTEMPTS`,
`PRELOOP_MIGRATION_RETRY_MIN_SECONDS`, `PRELOOP_MIGRATION_RETRY_MAX_SECONDS`),
so the same behaviour applies when migrations are run by hand in a pod.
Concurrent index overrides are
`PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS` and
`PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS`. Zero, negative,
non-integer or out-of-range values fall back to the finite defaults. The lock
budget must be shorter than the statement budget, which includes lock waits;
conflicting combinations fall back to both defaults so lock contention times
out first with a retryable SQLSTATE. The
deployment deadline remains unchanged. Per-statement limits do not guarantee
the total duration of multiple pending revisions.

Concurrent blocks log their start, elapsed duration and SQLSTATE on failure
through Alembic logging. An unsuccessful operation points to
`pg_stat_activity`, `pg_blocking_pids(pid)` and
`pg_stat_progress_create_index`: inspect transaction age, lock relationships
and build phase before retrying. Check `pg_index.indisvalid` and `indisready`
for interrupted builds. A current snapshot cannot identify a historical
blocker once its transaction has ended. Do not increase deployment timeouts
to hide a repeatedly canceled build.

## Data backfills run after the deploy

Helm waits on the `pre-upgrade` hook for its default five minutes. When that
runs out the release fails, but the Job keeps running and replacement pods pick
it up again, so a slow revision both breaks the deploy and keeps working
against live traffic. Schema changes (tables, triggers, indexes) fit that
budget. Rewriting history row by row does not: on a large `api_usage` table a
batch loop over every gateway request takes far longer than five minutes.

So revisions do not backfill data. A revision that needs one:

1. creates the schema, including any trigger that keeps the derived data
   current for new writes;
2. registers the backfill: a row in `agent_usage_backfill` with a `name`, a
   cursor (`last_usage_id`) and `ready = false`, inserted with
   `ON CONFLICT DO NOTHING` so re-running the revision never resets progress;
3. leaves readers on their previous query path until `ready` is true.

The batch statement lives in `preloop.models.backfills`. The sync scheduler
publishes `run_data_backfills` one minute after it starts and then every 15
minutes; a worker runs pending backfills in committed batches with
a 60-second budget per pass; a batch already running can extend this by its
statement timeout. Each pass occupies a shared worker slot, so the 15-minute
interval limits delays to webhooks, polling and reconciliation while history
catches up. The next pass resumes from the cursor. A pass with nothing pending
reads the small registry table and returns. Failed batches are logged and the
task raises so the worker reports the failure and redelivers the message.

Operators can also drive it by hand, for example to finish a backfill before
scaling the sync role down:

```bash
kubectl exec deploy/preloop-api -- python -m preloop.models.backfills --time-budget 3600
```

To check progress:

```sql
SELECT name, ready, last_usage_id FROM agent_usage_backfill;
```

A test (`backend/tests/models/test_no_backfill_loops_in_revisions.py`) fails
when a revision file contains a `while` loop, so a new batch loop in a revision
is caught in review.

## Application sessions must not idle in a transaction

A short lock timeout only helps if the lock eventually becomes free. A session
that is "idle in transaction" still holds an AccessShareLock on every table it
read, for as long as it idles, so a single long-lived reader can block a
migration indefinitely. Two paths used to do this and no longer do: the runner
control websocket ends its transaction before waiting for the next heartbeat,
the tracker polling worker ends its transaction before calling a tracker
API, and the hosted-agent monitor on the flow-execution worker ends its
transaction after each group of per-poll reads (stop and park requests,
tool-activity counts), so the agent status call, the NATS publishes and the
poll sleeps run with no transaction open.

New code that awaits anything other than the database (HTTP, Kubernetes,
NATS, embeddings, `asyncio.sleep`) while holding a `Session` should call
`preloop.models.db.session.release_transaction(db)` first. Read ids or plain
values, release, then do the slow work and any writes in short transactions.

### `idle_in_transaction_session_timeout` as a backstop

Postgres can terminate sessions that stay idle in a transaction for too long.
This is a safety net for a regression, not the fix: when it fires, the
application sees a dropped connection in the middle of its work, so the code
path still has to release its transaction itself. If you set it, keep it well
above any legitimate short transaction and scope it to the application role
rather than to the whole cluster, so the migration role and manual `psql`
sessions are unaffected:

```sql
ALTER ROLE preloop_app SET idle_in_transaction_session_timeout = '60s';
```

(`preloop_app` stands for whatever role the API and workers connect as.) The
setting applies to new connections only, so it takes effect as pods recycle
their pools.

If a future migration stalls, this is the first thing to check:

```sql
SELECT pid, state, now() - state_change AS idle_for, application_name, query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
ORDER BY state_change;
```

and the lock graph:

```sql
SELECT blocked.pid AS blocked_pid, blocking.pid AS blocking_pid,
       blocked.query AS blocked_query, blocking.query AS blocking_query
FROM pg_stat_activity blocked
JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking_pid ON TRUE
JOIN pg_stat_activity blocking ON blocking.pid = blocking_pid
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;
```

## When to drain the API first

The defaults are meant to make draining unnecessary. Drain anyway when:

- **a revision rewrites a large table** (a `DROP COLUMN` is cheap, an
  `ALTER COLUMN ... TYPE` that rewrites rows is not). Such a revision holds its
  ACCESS EXCLUSIVE lock for the length of the rewrite regardless of any
  timeout, and every query on that table waits;
- **blocking DDL has exhausted its retry budget**. A longer generic
  `lockTimeout` makes queued traffic wait longer. For concurrent indexes,
  inspect old writers/snapshots and tune only their separate budgets instead;
- **blocking DDL was canceled by `statement_timeout`** (SQLSTATE `57014` in
  the Job log). It is not retried within the container, on purpose;
- **the release notes say so** for a specific revision.

To drain, scale the serving deployments to zero, run the upgrade, then scale
back. Connected private runners reconnect on their own; in-flight flow
executions are re-dispatched by the stale-claim reaper.

```bash
kubectl scale deploy/preloop-api deploy/preloop-preloop-sync-worker-default --replicas=0
helm upgrade preloop ./helm/preloop -f values.yaml
kubectl scale deploy/preloop-api --replicas=<n>
kubectl scale deploy/preloop-preloop-sync-worker-default --replicas=<n>
```

## Reading a failed hook

```bash
kubectl logs job/preloop-migration-job
```

- `retrying in Ns` lines with a SQLSTATE mean lock contention. The upgrade is
  retrying safely. If it never converges, inspect old transactions and the
  blocking operation. Concurrent indexes can also be waiting on a legitimate
  older writer or snapshot; do not infer an idle-session bug from a timeout
  alone.
- Any other traceback is a revision that needs fixing. The database is left at
  the last revision that committed, which is a consistent state: the same
  upgrade can be re-run after the fix.
