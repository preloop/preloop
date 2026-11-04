# Platform Operations

Release publication gates, database connection pools, upgrade acceptance and API process diagnostics. Read this chapter when changing how Preloop is built, released, pooled or started. Deployment guides live under `docs/operations/`.

## Release publication gate

Version-tag publication is gated by [image SBOM and release audit validation](../release-sbom-audit.md). Backend, console, scanner and universal images carry inventories for both architectures, bound to immutable image digests and the source commit. The gate waits for the same tag's image and security workflows before publishing release assets.

## Database connection pools

Credential checks use a dedicated synchronous auth pool alongside the sync and
async request pools and the health engine. The auth pool adds its configured
base size to each API/gateway process's retained connection budget, and base
plus overflow to its ceiling. Helm values include this contribution; the pool
monitor reports it as `auth`. Bounded non-machine classification caching never
replaces route authentication; [restricted CI operations](../guide/flows/restricted-ci.md#credential-check-resilience)
documents its freshness and database-error fallback windows. Async session
teardown has a single shielded cleanup owner so caller cancellation cannot
interrupt rollback/close or start a concurrent close on the same session.

## Upgrade acceptance harness

The [upgrade acceptance harness](../operations/upgrade-acceptance.md) runs published
release initializers and Helm hooks against a synthetic corpus in disposable local
stacks. It stops old writers, preserves encryption settings, and verifies data,
identity and credential continuity. Candidate rehearsals remain separate from
acceptance of an actually published release candidate.

## API diagnostics

The API entry point enables Python fatal-error tracebacks on stderr (including
spawned Uvicorn reload/worker interpreters via `PYTHONFAULTHANDLER`) before
application startup so native faults retain thread-stack evidence in container
logs. These traces do not include frame locals. Setting `SENTRY_DSN` keeps error
reporting and transaction tracing enabled, but background stack profiling is
off by default. Operators may explicitly set `SENTRY_PROFILES_SAMPLE_RATE` to
a finite value from 0 to 1 after validating profiling with their runtime and
native extensions. Invalid values disable profiling and log a configuration
warning. Profiling is independent of the request-level `PROFILING_ENABLED`
middleware option.

API liveness-triggered restarts use a separate 60-second termination grace
(`api.livenessTerminationGracePeriodSeconds`, including the preStop sleep).
Planned rollouts retain the 900-second stream drain. This prevents an unhealthy
replica from spending the full rollout window draining before replacement.
The chart renders probe-level grace on Kubernetes 1.28 or newer; older
clusters retain the existing pod-level grace behavior.
