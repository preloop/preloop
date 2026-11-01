# PgBouncer (CNPG Pooler)

The chart can deploy PgBouncer in front of its CloudNativePG cluster through
the CNPG `Pooler` resource. It is off by default.

```yaml
database:
  pgbouncer:
    enabled: true
    instances: 2
    poolMode: transaction   # or session
    parameters:
      default_pool_size: "20"
      max_client_conn: "400"
```

This creates the Service `<cluster>-pooler-rw`, where `<cluster>` is
`database.cnpg.name` or, by default, `<fullname>-db` (for example release
`prod` renders `prod-preloop-db-pooler-rw`). The chart does not point the application at it:
`DATABASE_URL` still targets the cluster's `-rw` Service. It is not available
with `database.external=true`; the render fails instead.

## Connection budget without PgBouncer

Each process opens sync and async SQLAlchemy pools sized by
`DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW`, plus a one-connection health
engine and a small auth pool. The chart sets these per component under
`database.pool` and documents the budget against `max_connections` in
`values.yaml`. `DB_MONITORING_ENABLED` (on by default) exports pool gauges
over OpenTelemetry and logs a WARNING when a pod reaches 80% of its pool
ceiling. Lowering pool sizes or adding database capacity is the supported
fix today.

## Connection budget with PgBouncer

Each Pooler instance opens up to `default_pool_size` server connections per
database/user pair, so the Pooler itself uses up to
`instances x default_pool_size` connections (40 with the defaults). Count
them against `max_connections` alongside anything still connecting
directly. If you route the application through a session-mode pooler, every
client session holds one server connection for its lifetime, so the server
pool must cover the app's steady total from the `database.pool` budget in
`values.yaml` (roughly 150 to 190 with the chart defaults); with the default
40 server connections, clients would queue and hit `pool_timeout`. Raise
`default_pool_size` (or lower app pool sizes) before routing in session
mode.

## Why the application is not routed through it yet

PgBouncer in **transaction** mode hands a server connection to a client only
for the length of one transaction. Preloop currently depends on
session-scoped state in a few places:

- Session advisory locks (`pg_try_advisory_lock`) in the flow-execution
  triage lock, security maintenance, and issue lifecycle. Behind transaction
  pooling these locks would be held by, and released from, the wrong server
  connection. Transaction-scoped `pg_advisory_xact_lock` calls are fine.
- `LISTEN preloop_access` for access-rule cache invalidation. Notifications
  are delivered to a server connection, not to the client.
- asyncpg prepared statements. asyncpg caches named prepared statements per
  connection, which fails with "prepared statement does not exist" behind
  transaction pooling unless the engine passes `statement_cache_size=0`
  (or PgBouncer 1.21+ runs with `max_prepared_statements` set).
- The psycopg engines send `options=-c statement_timeout=...` as a startup
  parameter. PgBouncer rejects unknown startup parameters; adding `options`
  to `ignore_startup_parameters` makes the connection succeed but silently
  drops the timeout. Set `statement_timeout` on the database role instead.

**Session** mode avoids the advisory-lock, LISTEN and prepared-statement
problems, but the `options` startup parameter is rejected in every pool
mode (handled below). Session mode only caps concurrent
server connections; it does not multiplex. To use it, set
`poolMode: session` and supply a `DATABASE_URL` pointing at the pooler
Service through `database.urlFromSecret`, then add `options` to
`parameters.ignore_startup_parameters` and set `statement_timeout` on the
role, as above.

Transaction mode is the target once the items above route through a direct
connection; until then, deploy the Pooler only for evaluation or for other
clients.
