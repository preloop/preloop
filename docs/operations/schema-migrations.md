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
`alembic upgrade head`. Three things differ.

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

**A bounded retry loop** (`migrationJob.maxAttempts`, default 20, with
`migrationJob.retryMinSeconds` to `migrationJob.retryMaxSeconds` of jittered
backoff). Only lock contention is retried: deadlocks (`40P01`), lock timeouts
(`55P03`) and serialization failures (`40001`). A revision with a bug fails on
the first attempt with its own error, because retrying it twenty times would
only delay the report. So does a revision killed by the cluster
`statement_timeout` (`57014`): that one is too slow rather than unlucky, it
would fail identically on every attempt, and each attempt would hold the
exclusive lock for the full timeout. Drain the API for it instead.

Together these turn "the release fails after six long attempts" into "a
revision that loses a lock race gives up in five seconds and wins on the next
pass".

## Defaults

| Value | Default | What it controls |
| --- | --- | --- |
| `migrationJob.lockTimeout` | `5s` | How long one revision waits for a lock before giving up. |
| `migrationJob.maxAttempts` | `20` | Attempts at `upgrade head` before the Job fails. |
| `migrationJob.retryMinSeconds` | `3` | Lower bound of the jittered backoff. |
| `migrationJob.retryMaxSeconds` | `15` | Upper bound of the jittered backoff. |
| `migrationJob.backoffLimit` | `2` | Pod-level restarts, for a crashed container. |

The same settings are read from the environment
(`PRELOOP_MIGRATION_LOCK_TIMEOUT`, `PRELOOP_MIGRATION_MAX_ATTEMPTS`,
`PRELOOP_MIGRATION_RETRY_MIN_SECONDS`, `PRELOOP_MIGRATION_RETRY_MAX_SECONDS`),
so the same behaviour applies when migrations are run by hand in a pod.

## Application sessions must not idle in a transaction

A short lock timeout only helps if the lock eventually becomes free. A session
that is "idle in transaction" still holds an AccessShareLock on every table it
read, for as long as it idles, so a single long-lived reader can block a
migration indefinitely. Two paths used to do this and no longer do: the runner
control websocket ends its transaction before waiting for the next heartbeat,
the tracker polling worker ends its transaction before calling a tracker
API, and the hosted-agent monitor on the flow-execution worker ends its
transaction after the per-poll tool-activity read, before it publishes, asks
the agent for its status or sleeps until the next poll.

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
- **the retry budget is exhausted** and the Job has failed. Draining is the
  answer, not a longer `lockTimeout`: a longer wait makes the stall worse, not
  shorter;
- **a revision was cancelled by `statement_timeout`** (SQLSTATE `57014` in the
  Job log). It is not retried, on purpose;
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
  working as designed; if it never converges, something is idling in a
  transaction (see above) or a revision needs a drain.
- Any other traceback is a revision that needs fixing. The database is left at
  the last revision that committed, which is a consistent state: the same
  upgrade can be re-run after the fix.
