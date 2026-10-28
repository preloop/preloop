# OTLP export

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

For what tool/model record stores retain under redaction and reference-only
policies, see [Sensitive-data handling](sensitive-data.md#what-is-stored).

Preloop can export OpenTelemetry traces (and duration, token and cost metrics) for
governed model calls and MCP tool calls to any OTLP-compatible backend.
Export is **disabled by default**. Turning it on does not replace the
`ApiUsage` ledger or console cost views.

Spans follow [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
where they apply. Runtime session identity is emitted as
`gen_ai.conversation.id` (see [Runtime Session Identity](../architecture/gateway.md)).

This page covers telemetry Preloop sends out. To receive Claude Code,
Claude Desktop and Cowork telemetry in Preloop (the opposite direction), see
[Claude client telemetry ingest](clients/claude-telemetry-ingest.md).

Which attribute and metric names are stable, and how renames are
announced, is set out in the
[OTLP attribute stability policy](otlp-attribute-stability.md).

## Privacy

Raw prompts, completions, and tool arguments are **not** attached to span
attributes. Token counts, recorded cost, model/provider names, HTTP status,
account id, and session id are. There is no payload debug flag in this
release.

## Enablement

Set environment variables on the API and gateway processes (Helm injects
the same `otlp.*` values into both deployments):

| Variable | Helm value | Default | Notes |
| -------- | ---------- | ------- | ----- |
| `OTLP_ENABLED` | `otlp.enabled` | `false` | Must be true and `OTLP_ENDPOINT` must be set |
| `OTLP_ENDPOINT` | `otlp.endpoint` | empty | Also accepts `OTEL_EXPORTER_OTLP_ENDPOINT` |
| `OTLP_PROTOCOL` | `otlp.protocol` | `http/protobuf` | `http/protobuf` or `grpc`. Also accepts `OTEL_EXPORTER_OTLP_PROTOCOL` |
| `OTLP_HEADERS` | `otlp.headers` / `otlp.headersSecret` | empty | `key=value,key2=value2`. Also accepts `OTEL_EXPORTER_OTLP_HEADERS` |
| `OTLP_SERVICE_NAME` | `otlp.resource.serviceName` | `preloop` | Resource `service.name` |
| `OTLP_SERVICE_NAMESPACE` | `otlp.resource.serviceNamespace` | empty | Resource `service.namespace` |
| `OTLP_DEPLOYMENT_ENVIRONMENT` | `otlp.resource.deploymentEnvironment` | empty | Falls back to `ENVIRONMENT` |
| `OTLP_SAMPLER_RATIO` | `otlp.samplerRatio` | `1.0` | Parent-based TraceIdRatioBased ratio in `[0, 1]` |

HTTP exporters append `/v1/traces` and `/v1/metrics` when those suffixes
are missing. Point `OTLP_ENDPOINT` at the collector base URL or at the
full traces URL. If the URL already ends in `/v1/traces` (Datadog direct
intake), metrics export is skipped so Preloop does not construct
`.../v1/traces/v1/metrics`.

Exporter errors are logged. The user-facing gateway or MCP response is
unchanged.

Helm example (ingest headers from an existing Secret):

```yaml
otlp:
  enabled: true
  endpoint: "http://otel-collector:4318"
  protocol: http/protobuf
  headersSecret:
    name: preloop-otlp-headers
    key: otlp-headers
  resource:
    serviceName: preloop
    deploymentEnvironment: production
  samplerRatio: 1.0
```

Equivalent environment:

```bash
export OTLP_ENABLED=true
export OTLP_ENDPOINT="http://otel-collector:4318"
export OTLP_PROTOCOL="http/protobuf"
export OTLP_HEADERS="Authorization=Bearer ${COLLECTOR_TOKEN}"
export OTLP_SERVICE_NAME="preloop"
export OTLP_DEPLOYMENT_ENVIRONMENT="production"
```

Docker Compose (`docker-compose.yml` for development and the
`docker-compose.release.yaml` stack the OSS installer downloads) forwards the
`OTLP_*` variables above (except `OTLP_SERVICE_NAMESPACE` and
`OTLP_SAMPLER_RATIO`) from `.env` to the `api` and `gateway` services. Export
stays off while `OTLP_ENABLED` is false or `OTLP_ENDPOINT` is empty. Add the
lines to `.env` next to the compose file:

```bash
OTLP_ENABLED=true
OTLP_ENDPOINT=http://otel-collector:4318
OTLP_PROTOCOL=http/protobuf
OTLP_HEADERS=Authorization=Bearer <token>
OTLP_SERVICE_NAME=preloop
OTLP_DEPLOYMENT_ENVIRONMENT=production
```

Then recreate the services so they pick up the new environment:

```bash
docker compose up -d
```

## Sampling and cardinality

The SDK sampler is parent-based with `OTLP_SAMPLER_RATIO` (default `1.0`
when export is enabled). High-volume instances should sample at the
collector, or lower the ratio (for example `0.1`) when no collector is
present and you export directly to a vendor.

High-cardinality attributes on traces:

- `gen_ai.conversation.id` (runtime session id)
- `preloop.api_usage.id`
- `preloop.account.id`

Duration, token and cost metrics use operation, provider, and model only
(plus `gen_ai.token.type` on the token histogram). Do not add
payloads to attributes; that would raise both privacy risk and cardinality.

## Destinations

Vendor docs checked 2026-08-28. Native vendor SDKs are out of scope when
OTLP reaches the same destination.

### Generic OpenTelemetry Collector

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  batch: {}

exporters:
  debug:
    verbosity: basic

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
```

Point Preloop at the collector:

```bash
export OTLP_ENABLED=true
export OTLP_ENDPOINT="http://otel-collector:4318"
export OTLP_PROTOCOL="http/protobuf"
```

gRPC:

```bash
export OTLP_ENABLED=true
export OTLP_ENDPOINT="http://otel-collector:4317"
export OTLP_PROTOCOL="grpc"
```

### Langfuse OTLP ingest

Docs: [Langfuse OpenTelemetry](https://langfuse.com/integrations/native/opentelemetry)
(checked 2026-08-28). Langfuse accepts OTLP over HTTP (`HTTP/protobuf` or
`HTTP/JSON`). gRPC is not supported. Authenticate with Basic auth from the
project public and secret keys, and send `x-langfuse-ingestion-version: 4`
for real-time ingest.

```bash
# echo -n "pk-lf-...:sk-lf-..." | base64
export OTLP_ENABLED=true
export OTLP_ENDPOINT="https://cloud.langfuse.com/api/public/otel"
export OTLP_PROTOCOL="http/protobuf"
export OTLP_HEADERS="Authorization=Basic ${AUTH_STRING},x-langfuse-ingestion-version=4"
```

US region: `https://us.cloud.langfuse.com/api/public/otel`. Self-hosted
Langfuse v3.22.0 or later: `http://localhost:3000/api/public/otel`.

Helm:

```yaml
otlp:
  enabled: true
  endpoint: "https://cloud.langfuse.com/api/public/otel"
  protocol: http/protobuf
  headersSecret:
    name: langfuse-otlp
    key: otlp-headers
```

Secret value:

```text
Authorization=Basic AUTH_STRING,x-langfuse-ingestion-version=4
```

### Datadog OTLP ingest

Docs: [Datadog OTLP traces intake](https://docs.datadoghq.com/opentelemetry/setup/otlp_ingest/traces/)
(checked 2026-08-28). Direct intake supports `http/protobuf` (gRPC is not
supported on this endpoint). Required headers: `dd-api-key` and
`compute_stats=true` for trace metrics.

US1 traces URL: `https://otlp.datadoghq.com/v1/traces`. Use the traces
intake URL for your Datadog site from that page (US3, US5, EU, AP1, AP2,
US1-FED).

```bash
export OTLP_ENABLED=true
export OTLP_ENDPOINT="https://otlp.datadoghq.com/v1/traces"
export OTLP_PROTOCOL="http/protobuf"
export OTLP_HEADERS="dd-api-key=${DD_API_KEY},compute_stats=true"
```

Helm:

```yaml
otlp:
  enabled: true
  endpoint: "https://otlp.datadoghq.com/v1/traces"
  protocol: http/protobuf
  headersSecret:
    name: datadog-otlp
    key: otlp-headers
```

Secret value:

```text
dd-api-key=YOUR_DATADOG_API_KEY,compute_stats=true
```

For production volume, Datadog recommends an OpenTelemetry Collector or
Datadog Agent in front of intake.

### Gigapipe (formerly qryn)

Docs: [Gigapipe configuration](https://github.com/metrico/gigapipe/blob/main/docs/configuration.md),
[OTLP over gRPC](https://github.com/metrico/gigapipe/blob/main/docs/otlp-grpc.md)
(checked 2026-10-27). Gigapipe accepts OTLP traces and metrics directly on its
HTTP port (default `3100`), over HTTP/protobuf or gRPC (h2c, same port). No
collector is needed.

```bash
export OTLP_ENABLED=true
export OTLP_ENDPOINT="http://gigapipe:3100"
export OTLP_PROTOCOL="http/protobuf"   # or grpc
# Basic auth, if GIGAPIPE_LOGIN/GIGAPIPE_PASSWORD are set:
# export OTLP_HEADERS="Authorization=Basic ${AUTH_STRING}"
```

In Grafana, add Gigapipe as a Tempo datasource (traces, TraceQL) and as a
Prometheus datasource (`gen_ai_client_operation_duration_seconds_*`,
`gen_ai_client_token_usage_*`, and the cost counter), both at
`http://gigapipe:3100`. Example queries:

- TraceQL: `{ resource.service.name = "preloop" && span.gen_ai.operation.name = "chat" }`
- TraceQL: `{ span.gen_ai.conversation.id = "<runtime session id>" }` (one agent session)
- PromQL: `histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate(gen_ai_client_operation_duration_seconds_bucket[5m])))`
- PromQL: `sum by (gen_ai_request_model, gen_ai_token_type) (rate(gen_ai_client_token_usage_sum[5m]))`

Token counts and cost are also span attributes (`gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `preloop.usage.estimated_cost_usd`), visible in
the trace view. MCP tool spans appear only for calls made with a runtime
session token.

## What is exported

| Signal | When | Notes |
| ------ | ---- | ----- |
| Span per completion/response | After the `ApiUsage` row is written | Streaming and non-streaming. Attributes include tokens and `preloop.usage.estimated_cost_usd` matching that row. |
| Span per `GET /openai/v1/models` | After the model list is built | No conversation id. |
| Span per MCP tool call | After execution, only if a runtime session id is present | `gen_ai.operation.name=execute_tool`. Linked to the last in-process model span for that session when available. Tool arguments are never attributes. |
| Duration histogram | Same as the spans above | `gen_ai.client.operation.duration` (seconds), low-cardinality attributes only. |
| Token usage histogram | Per model call span | `gen_ai.client.token.usage` (`{token}`), one point per call for `gen_ai.token.type=input` and one for `output`. |
| Cost counter | Per model call with a non-zero recorded cost | `preloop.usage.cost` (`USD`), equal to `preloop.usage.estimated_cost_usd` on the span. |

Internal model-gateway events (`model_gateway_events.py`) are unchanged.
OTLP is an additional export.
