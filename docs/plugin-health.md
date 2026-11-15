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
`preloop_plugin_status` JetStream key-value bucket on the existing NATS service.
It requires JetStream key-value create/read/write access. Disabled or absent
extensions do not add a status connection. Process records contain plugin names
and loaded booleans, task counters, task filters and timestamps. They contain no
configuration, exception text, task arguments or credentials.

`GET /api/v1/health/plugins` reads that bucket and requires
`Authorization: Bearer <operator token>`, matching `PLUGIN_STATUS_OPERATOR_TOKEN`
on the API. This credential authorizes only this read-only surface; ordinary
account credentials do not grant access. Missing server configuration returns
503; invalid credentials return 401. Health monitors can use the same endpoint.
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

Drift email dispatch claims use the existing `billing_operation` table through
CRUD, scoped to account/provider/complete UTC day/material cost fingerprint.
Repeated costs do not send again; changed actuals or estimates can send once.
Claims commit before SMTP and are never reclaimed after an uncertain handoff.
A mail failure can therefore need operator investigation rather than automatic
retry. No database migration is required.
