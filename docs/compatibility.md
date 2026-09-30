# Compatibility policy

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Operators upgrading a self-hosted install, and anyone scripting against
Preloop, need to know which surfaces are stable and how a change to them
is announced. This page is that contract. It is a prerequisite for 1.0.0.

Preloop versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
A minor release line is one `X.Y` (for example every `0.18.z` patch). The
rules below apply only to the public surfaces.

## Public surfaces

- REST API under `/api/v1` (the paths and schemas in `openapi.yaml`).
- CLI commands and flags (`preloop ...`), including exit codes and
  machine-readable output.
- Webhook payloads Preloop sends and the event types it accepts.
- The runtime plugin protocol (`preloop.agent_control.v1`) used by the
  published sidecars.
- Helm chart values.
- `result.json` and evidence pack formats produced by flow executions.
- Database migrations: forward-only, applied by the upgrade hook. See
  [schema migrations](operations/schema-migrations.md).

## What is not public

Anything not listed above is not public and can change in any release.
That includes internal Python modules, database tables, console HTML, and
undocumented endpoints.

## The rule

1. Within a minor release line, changes to a public surface are additive
   only (new fields, new optional flags, new endpoints, new event types).
2. A removal or incompatible change is announced as a deprecation at least
   one minor release ahead, in the changelog and, where the surface allows
   it, at runtime (a response header, a CLI warning, a log line).
3. The removal ships no earlier than two minor releases after the
   deprecation. A surface deprecated in `0.18.0` is still present in
   `0.19.x` and is not removed before `0.20.0`.
4. Security fixes may break this rule when there is no safe additive path.
   The changelog says so and names the affected surface.
5. Migrations are forward-only. Downgrade means restore from backup.

## API versioning

Pending. What `/api/v1` commits to, and how the unprefixed OAuth, MCP and
gateway paths are versioned, is tracked in its own issue:
[#977](https://github.com/preloop/preloop/issues/977). That issue is
linked from the
[1.0.0 readiness checklist](https://github.com/preloop/preloop/issues/978).
Until that decision lands, this section stays pending.
