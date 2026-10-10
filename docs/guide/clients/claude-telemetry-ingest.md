# Claude client telemetry ingest (OTLP)

Editions: OSS, Cloud, Enterprise.

Claude Code, Claude Desktop and Cowork export OpenTelemetry metrics and log
events (and, when enabled, traces) over OTLP/HTTP. Preloop can receive those
exports and turn them into usage rows, so you see:

- cost and token counts for Claude traffic that never went through the
  Preloop model gateway;
- per-request telemetry (session id, prompt id, client, reported cost) on
  the gateway's own usage rows, matched by request;
- Desktop and terminal sessions grouped with their gateway requests.

This page is about telemetry flowing **into** Preloop. For Preloop's own
traces and metrics flowing out to your collector, see
[OTLP export](../observability-otlp.md).

## Endpoints

| Signal  | Path                                   | Stored                     |
|---------|----------------------------------------|----------------------------|
| Metrics | `POST /api/v1/telemetry/otlp/v1/metrics` | cost and token counters    |
| Logs    | `POST /api/v1/telemetry/otlp/v1/logs`    | `api_request`, `api_error` |
| Traces  | `POST /api/v1/telemetry/otlp/v1/traces`  | acknowledged, not stored   |

Use `<your Preloop URL>/api/v1/telemetry/otlp` as the exporter base URL; the
standard per-signal paths follow it.

- Content types: `application/x-protobuf` (the default for Desktop, Cowork
  and the apps gateway) and `application/json`. `Content-Encoding: gzip` is
  accepted.
- Limits: 4 MiB per request after decompression, 10,000 data points or log
  records per request, attribute values cut to 1 KiB, at most 64 attributes
  read per record.
- Responses follow OTLP/HTTP: `200` with an empty `Export*ServiceResponse`
  in the request's encoding. When records were dropped (outside the
  allowlist below, or over the per-request limit) the response carries
  `partial_success` with the `rejected_*` count. `400` undecodable body,
  `401` missing or invalid key, `403` key without the scope, `413` too
  large, `415` other content types, `429` with `Retry-After` when the
  per-key rate limit is hit (600 requests per minute by default,
  `PRELOOP_OTLP_INGEST_RATE_LIMIT` to change it).

## 1. Create an ingest key

An account admin creates an API key with the single scope
`telemetry:ingest`:

```bash
curl -s -X POST "$PRELOOP_URL/api/v1/auth/api-keys" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "claude telemetry ingest", "scopes": ["telemetry:ingest"]}'
```

Only admins can grant the scope. A key whose only scope is
`telemetry:ingest` can post OTLP exports and nothing else: every other REST
route, the MCP endpoint, the model gateway and console channels refuse it.
Send it as `Authorization: Bearer <key>` or `x-api-key: <key>`.

## 2a. Behind a Claude apps gateway

Add Preloop as a `telemetry.forward_to` destination in the gateway config.
The gateway relays each export verbatim, stamped with the signed-in user's
identity, and pushes the telemetry settings to connected clients:

```yaml
telemetry:
  forward_to:
    - url: https://preloop.example.com/api/v1/telemetry/otlp
      headers:
        Authorization: Bearer ${PRELOOP_TELEMETRY_KEY}
      metrics: true
      logs: true     # needed for per-request matching
      traces: false  # accepted but not stored
```

The gateway's default is metrics only. With metrics only, Preloop builds
hourly aggregates per session and model (see below); turn `logs` on to get
per-request rows and enrichment.

## 2b. Direct from Claude Code or Desktop

Without a gateway, set the standard exporter variables (in managed settings
for a fleet):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT=https://preloop.example.com/api/v1/telemetry/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <ingest key>"
```

`http/json` works too. gRPC is not supported.

## What is stored, and what is dropped

Preloop keeps an allowlist and drops everything else before it reaches the
database. Dropped records are only counted.

**Log events.** Only `claude_code.api_request` and `claude_code.api_error`.
Of their attributes, only: `request_id`, `client_request_id`, `session.id`,
`prompt.id`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`cache_creation_tokens`, `cost_usd`, `duration_ms`, `status_code`,
`user.email`, `user.id`, `enduser.id`, `enduser.sub`, `user.groups`,
`identity.source`, `terminal.type`, `service.name`, `service.version`,
`organization.id`. Every other event (`user_prompt`, `tool_result`,
`tool_decision`, `api_request_body`, `api_response_body` and the rest) is
dropped, as is any other attribute on an allowed event, including a prompt
text attribute.

**Metrics.** Only `claude_code.cost.usage` and `claude_code.token.usage`
(with their `type` and `model`) and `claude_code.session.count`. Delta and
cumulative temporality are both handled; cumulative series are turned into
deltas with per-series state that expires after 24 hours.

**Traces.** Acknowledged so exporters do not error, counted, not stored.

Preloop never stores prompts, tool inputs, commands, file paths or traces
from this endpoint.

!!! warning "Logs and traces are sensitive at the source"
    Claude Code log events and traces can carry full Bash commands, tool
    inputs and file paths (and prompt text when `OTEL_LOG_USER_PROMPTS` is
    set). Preloop drops them on arrival, but they still leave the developer
    machine and pass through the apps gateway. Enable logs and traces only
    toward destinations whose access controls and retention fit that data.

## How usage rows are built (never counted twice)

- **An `api_request` event that matches a gateway request** enriches that
  gateway usage row (`meta_data.telemetry`: session id, prompt id, client,
  identity and the client's reported `cost_usd`) and creates nothing.
  Matching tries, in order:
    1. the client's `x-client-request-id`, which the Preloop Anthropic
       gateway records on its usage rows (`meta_data.client_request_id`);
    2. `upstream_request_id == request_id`;
    3. a signature: same account, completion time within 60 seconds, same
       model (a dated id on one side is fine), same output token count, same
       person (email or IdP subject, or a gateway row that names no person),
       and a gateway row not enriched yet. The closest in time wins.
       `meta_data.telemetry.match` says which rule matched.

  Why the third rule exists: the `request_id` a client reports comes from
  the `request-id` response header, which Preloop does not relay, and
  behind a Claude apps gateway (2.1.288 in our harness) the relayed events
  carry neither `request_id` nor `client_request_id`, and the gateway does
  not forward `x-client-request-id` or `x-claude-code-session-id` upstream.
- **An unmatched `api_request`** creates one usage row with
  `usage_source = imported`, `cost_source = telemetry_estimate`,
  `meta_data.source = otlp`, the reported tokens and
  `estimated_cost = cost_usd`.
- **A gateway request recorded later** for the same request removes the
  telemetry row in the same transaction and keeps its telemetry on the
  gateway row. One request, one row.
- **Metrics** create rows only when the session has neither log events nor
  gateway rows, and the person (email or IdP subject) has no gateway rows
  in that UTC hour: one aggregate per (session, model, UTC hour), with
  `meta_data.aggregate = true`. The person rule exists because behind an
  apps gateway the gateway rows carry no client session id, so a session
  cannot be matched; Preloop prefers an undercount to a double count. If
  the session's log events arrive later, its aggregates are removed in
  favour of the per-request rows. Logs and metrics of one session are
  serialized, so concurrent batches cannot both write.
- **Retries are no-ops.** Every stored record has a sha256 dedup key over
  account, signal, resource attributes, scope, record time and request id
  (or metric name and series attributes), enforced by a unique index. Dedup
  keys are kept 7 days.

Rows created from telemetry follow the normal usage retention policy.

## Identity and sessions

- Desktop and Cowork send `enduser.sub`; terminal sessions signed in
  through the apps gateway send the subject as `user.id` with
  `identity.source: gateway-oidc`. `user.id` is used only with that marker
  (in our 2.1.288 harness run the marker was absent, so terminal events
  resolved no subject; their email still matched gateway rows). Together with `user.email` it resolves a
  gateway subject keyed to the ingest key, the same record the trusted
  upstream gateway path uses. The email links a subject to an existing
  member only; no users or members are created. Desktop's `user.id` is an
  anonymous install id and is never used.
- `session.id` maps onto the runtime session the gateway recorded for the
  same Claude Code session (`x-claude-code-session-id`), so telemetry rows
  and gateway rows of one session show up together in the existing sessions
  and cost views. A session the gateway never saw keeps its id on the row
  (`conversation_id`) without a runtime session. Behind an apps gateway the
  session header does not reach Preloop, so gateway rows there are grouped
  per person and day instead and telemetry rows join them only through the
  request match above.
