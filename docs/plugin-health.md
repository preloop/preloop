# Plugin startup and worker health

`preloop-sync worker` (including `--tasks` and `--exclude-tasks`), scheduler
and monitor discover plugins before starting their service loops. Installed
proprietary extensions are enabled unless `DISABLE_RBAC=true` or
`DISABLE_PROPRIETARY_PLUGINS=true` (also `1`/`yes`). An absent proprietary
package is a normal OSS installation. Import, dependency and service-registration
failures inside an installed package log the plugin name and cause at ERROR and
terminate startup. A plugin is published only after its components register.
Worker startup does not invoke API plugin startup hooks, which can start jobs.

Missing handlers for the five scheduled plugin tasks warn once per task per
process and count every skipped invocation. Successful handler results record
completion timestamps; skipped or failed shims do not advance them. Schedules
and job bodies are unchanged.

Enabled proprietary processes publish a heartbeat every 30 seconds to the
`preloop_plugin_processes` JetStream key-value bucket on the existing NATS service.
Its records expire after 120 seconds and storage is bounded. Durable job
deadlines/completions use the separate `preloop_plugin_status` bucket without
a TTL. Reporting requires JetStream key-value create/read/write access and
retries independently: an unavailable status bucket logs an error but does not
prevent task processing. The probe detects missing reporting coverage. Disabled
or absent extensions do not add a status connection. Process records contain plugin names
and loaded booleans, task counters, task filters and timestamps. They contain no
configuration, exception text, task arguments or credentials.

`GET /api/v1/health/plugins` reads that bucket and requires
`Authorization: Bearer <operator token>`, matching `PLUGIN_STATUS_OPERATOR_TOKEN`
on the API (a secret-typed setting that also loads from native `.env` files).
Both compose API services pass the variable from `.env`. For Helm, use `extraEnv`
with `name: PLUGIN_STATUS_OPERATOR_TOKEN` and a `valueFrom.secretKeyRef` pointing
to the operator-managed secret. Keep this credential distinct from account keys.
This credential authorizes only this read-only surface; ordinary
account credentials do not grant access. Missing server configuration returns
503; invalid credentials return 401. An unreachable broker returns 503; a
reachable broker with no process bucket yet returns an empty fleet. Health monitors can use the same endpoint.
Process records older than 120 seconds are excluded. Configure a probe's expected
worker count to detect partial fleet coverage.

Scheduler status uses the actual registered jobs' next fire times: hourly
session digest sweep, configured Stripe reconciliation (six hours by default),
daily provider billing ingestion, Monday 07:00 UTC review and Monday 09:00 UTC
digest. An unsatisfied expected time persists across scheduler restarts; a
successful completion releases it for the next fire. These records survive
process restarts in JetStream storage. Fresh installations wait for their first
scheduled fire, with five minutes allowed for task delivery/completion. Existing
installations begin accumulating evidence when this version first starts.

Policy loading now resolves `cel_validation_service` through the registry.
The former working-directory-dependent import dates to commit `27967afc45`
(2026-02-01). Where the top-level `plugins` package was unavailable, YAML/JSON
policy loading skipped the optional CEL syntax checks on tool conditions
(explicit `cel` or detected CEL syntax), and model I/O conditions added by `4920838a1c` (2026-08-28). Schema,
reference and simple-condition checks still ran; runtime policy evaluation is a
separate path. Code establishes the bypass, not which deployed policies used it.

The core drift email persistence primitive uses the existing `billing_operation`
table through CRUD, scoped to account/provider/complete UTC day/material cost fingerprint.
The companion Enterprise billing change calls this primitive before email;
deduplication is effective only after both changes are integrated. The caller
finishes ingestion commits before claiming. Repeated costs then do not send
again; changed actuals or estimates can send once.
Claims commit before SMTP and are never reclaimed after an uncertain handoff.
A definite or uncertain mail failure can therefore need operator investigation
rather than automatic retry. Claims also commit the caller session on duplicates.
No database migration is required.
