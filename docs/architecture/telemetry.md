# Telemetry and Activity

OpenTelemetry export and ingest, optional activity and analytics primitives for extensions, vendor organization activity imports, and recording timeline synchronization.

## Telemetry export

Optional OpenTelemetry export (`preloop.services.otel_export`, disabled by
default) emits GenAI spans for governed model calls and MCP tool calls to any
OTLP endpoint, carrying `gen_ai.conversation.id` when a runtime session id is
present. Token and cost attributes match the `ApiUsage` row for that request;
prompts, completions and tool arguments are not attached. Exporter errors are
logged and never fail the user-facing call. It supplements the `ApiUsage`
ledger rather than replacing it. See [OTLP export](../guide/observability-otlp.md).

## Telemetry ingest

The opposite direction: `POST /api/v1/telemetry/otlp/v1/{metrics,logs,traces}`
(`preloop.api.endpoints.telemetry_otlp`, `preloop.services.otlp_telemetry`)
receives Claude Code, Claude Desktop and Cowork OTLP/HTTP exports, directly
or relayed by a Claude apps gateway's `telemetry.forward_to`. A key scoped
only to `telemetry:ingest` reaches nothing else. An allowlist keeps
`api_request`/`api_error` events and the cost, token and session metrics
and drops everything that could carry prompts, commands or paths. Events
enrich the matching gateway usage row (by `x-client-request-id`) or create
one `telemetry_estimate` row, which a later gateway row for the same request
replaces in its own transaction; metrics build hourly aggregates only for
sessions with no log events and no gateway rows. Dedup keys in
`telemetry_ingest_dedup` make exporter retries no-ops. See
[Claude client telemetry ingest](../guide/clients/claude-telemetry-ingest.md).

## Platform analytics extension primitives

`models.crud.admin_analytics` provides windowed model-ledger and report-event
aggregations for optional platform administration extensions. Platform-funded
usage and legacy built-in model usage use separate partial indexes; explicit
account funding never falls through to legacy classification. Gateway requests
snapshot funding and purpose in usage metadata. Weekly email preferences include
an audited account tracking switch, and the console renders it only when an
extension advertises `report_engagement_tracking`. Core does not serve public
tracking or cross-account analytics endpoints.

## Optional live activity foundations

Core accepts content-free prompt and turn timestamps through backend APIs and
stores them alongside bounded activity rollups, presence transitions and worker
watermarks through its CRUD layer. CLI and runtime-plugin hook producers are
outside this foundation. Hook timestamps stay out of the session replay timeline
and remain available to activity aggregation. Optional
Cloud and Enterprise extensions own derivation and authenticated board APIs;
core keeps the capability off by default. Presence realtime events require
`view_users` for another person or device, with an own-subject exception.
The Fleet Map remains the console visualization; this foundation adds no
Activity page, navigation entry or frontend component.

Instance-provided built-in models are enabled, non-retired global model rows;
selection and Fleet discovery use ownership/configuration rather than a fixed
provider alias. Custom model discovery applies the operator endpoint CIDR policy
and pins each request to a checked IP while preserving Host/TLS identity.
Local inference pricing defaults to zero after explicit prices; unknown remote
models remain unpriced. See [local model operations](../guide/runner-local-models.md).

Residual credential storage uses the existing SecretReference backend for model
and webhook values, and hash-only storage for all Preloop API keys. A backfill
revision clears the three legacy columns; a later release removes them once no
pod of the previous release can still map them. See the
[credential storage upgrade guide](../operations/credential-storage-upgrade.md)
for rolling-upgrade behaviour, encryption-key continuity and rollback limits.

## Recording timeline synchronization

Session artifacts optionally store typed `media_timing` independently of labels.
`services/media_sync.py` resolves exact step offsets, producer timestamps and
median-corrected server timestamps using metadata-only CRUD reads. The same
resolver feeds `GET /runtime-sessions/{id}/media-sync` and signed evidence
exports. Browser-step receipt times are server-owned; the migration backfills
existing rows from activity creation time. Timing PATCH writes and its audit
record commit atomically under the artifact account/session authorization.

The Lit `session-recording-player` lives in the session observer, outside Talk
routing and transcript construction. It uses the existing authorized artifact
cache, independent mappings per recording, and a default-on
`syncedRecordingPlayer` host option. Unmapped moments never clamp; GIFs use a
plain-image fallback. Browser Use and Skyvern producers emit timing through the
existing deposit API. See [the recording contract](../guide/artifacts.md#recordings-synchronized-to-the-timeline).

## Optional vendor organization activity

The Copilot daily importer exposes the `copilot_organization_metrics` service
hook after reading seats. An installed extension may import daily aggregate
reports through the existing read-only client; its service and endpoints live
outside core. Account-scoped snapshots and governed-owner lookups remain in
`preloop.models.crud`. The OSS Lit `copilot-activity-panel` is mounted in Cost
and Agents only when `copilot_seat_activity` is enabled, and hides data when
the extension denies permissions or entitlement. Vendor counters never enter
gateway token totals. Nullable premium fields preserve unknown values through
parsing, aggregation, API schemas and display. Durable per-day billing status
snapshots retain retry work independently of the adoption cursor; unavailable
days replace stale spend and make incomplete range totals unknown. Optional
extension failures are isolated from core imports with a sanitized warning.

[Upstream MCP OAuth](upstream-mcp-oauth.md) discovers protected resources and their selected authorization server, stores encrypted single-use authorization transactions, and coordinates token refresh across workers. The Lit tools setup exposes connection state and local disconnect; transport recovery never replays a dispatched tool call.

Every upstream MCP session built by `MCPClient` (`services/mcp_client_pool.py`) carries the identity headers defined in `services/upstream_identity.py`: `User-Agent: Preloop/<version>` (shared with the model path), `X-Preloop-Correlation-Id` equal to the audit `correlation_id` on proxied tool calls, and an opt-in `X-Preloop-Instance`. Per-server `auth_config` flags remove them or add the instance id; client headers are never forwarded.

Restricted CI's optional shared-flow selector resolves exactly one repository from
the principal's immutable project binding before minting a read lease. Tool audit
observations are stored as bounded canonical chunks in `flow_execution_log`,
separate from clipped console rows and never broadcast on realtime channels. A
new evidence action reads only an owned execution, with hashes and explicit
coverage/redaction flags. STOPPED writes queue the existing principal-filtered
completion outbox transactionally. Codex's opt-in native result schema uses CLI
structured output plus the existing capture-time validation gate; protocol
translation preserves the requested schema or fails explicitly.

Completed human-owned reviews can be exported through an explicit, expiring
`execution_result_grant`, without adopting their execution into a CI principal.
The CRUD layer binds current owner consent to original provider revisions and
result/event digests; immutable grant fields and monotonic revocation are guarded
in PostgreSQL. Export checks authority before and after a bounded provider read.
The separate publisher verifies the public receipt, exact head/base and stable
publication marker, then requires independent provider read-back. It neither
launches a model nor receives publication authority from the export grant.
