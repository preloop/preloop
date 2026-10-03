# OTLP attribute stability policy

Editions: OSS, Cloud, Enterprise.

This page states which OTLP span attributes and metrics you can build
dashboards and alerts on, and how changes to them are announced. It covers
what [OTLP export](observability-otlp.md) emits. The machine-readable list
is `backend/preloop/services/otel_attributes.py`; a unit test fails if the
exporter emits a name that is not listed there or if the tables below
disagree with it.

## Stability levels

**Stable.** The attribute or metric name and its meaning are kept within a
major version. Names in the `preloop.*` namespace listed below are stable,
plus `http.response.status_code`, which follows the stable OpenTelemetry
HTTP conventions.

**Experimental.** `gen_ai.*` names follow the upstream
[OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/),
which are still in Development upstream. The `db.client.connections.*` pool
gauges use names from the
[OpenTelemetry database conventions](https://opentelemetry.io/docs/specs/semconv/database/)
in their earlier, pre-stable form. Both groups track upstream changes.

**Not covered.** Span names, span kinds, links between spans, resource
attributes other than `service.name`, instrumentation scope names, and
anything not listed on this page. These may change in any release.

New attributes and metrics may appear in any release. Consumers should
ignore names they do not know.

## Renames and removals

For a stable name:

1. The change is announced in `CHANGELOG.md` under a "Telemetry" heading at
   least one minor release before the old name stops being emitted.
2. During the overlap both the old and the new name are emitted with the
   same value. The overlap lasts at least two minor releases or 60 days,
   whichever is longer.
3. The old name is removed only after the overlap ends, and the removal is
   noted in the CHANGELOG again.

A change to the meaning or unit of a stable name is treated as a rename: a
new name is introduced and the old one goes through the window above.

## Adopting upstream GenAI convention changes

When upstream renames a `gen_ai.*` attribute or metric, Preloop emits both
the old and the new name for the same overlap as a stable rename and
announces it under "Telemetry" in the CHANGELOG. An opt-in switch, following
the upstream `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`
pattern, selects new names only. The switch ships with the first upstream
rename that Preloop adopts. If upstream declares the GenAI conventions
stable, the adopted names move to the stable table.

## Metrics

Metric names and their dimension (attribute) sets follow the same rules as
span attributes. Metric dimensions in the `preloop.*` namespace (for example
`preloop.db.engine`) are stable even when the metric itself is
experimental. Adding a dimension is allowed at any time; removing or
renaming one goes through the deprecation window.

## Stable span attributes

| Attribute | Meaning |
| --------- | ------- |
| `preloop.account.id` | Preloop account id that owns the request. |
| `preloop.api_usage.id` | Id of the ApiUsage ledger row behind the span. |
| `preloop.usage.estimated_cost_usd` | Recorded cost in USD, equal to the ApiUsage row. |
| `preloop.usage.cost_source` | Where the recorded cost came from. |
| `preloop.mcp.server_name` | MCP server that served a tool call. |
| `http.response.status_code` | HTTP status of the governed call. |

## Experimental span attributes

| Attribute | Meaning |
| --------- | ------- |
| `gen_ai.operation.name` | GenAI operation (chat, embeddings, execute_tool). |
| `gen_ai.provider.name` | Model provider name. |
| `gen_ai.request.model` | Requested model alias. |
| `gen_ai.conversation.id` | Preloop runtime session id. |
| `gen_ai.usage.input_tokens` | Input token count. |
| `gen_ai.usage.output_tokens` | Output token count. |
| `gen_ai.response.finish_reasons` | Finish reasons reported by the model. |
| `gen_ai.tool.name` | Name of the executed tool. |
| `gen_ai.tool.type` | Tool type (function). |

## Metrics table

| Metric | Level | Dimensions |
| ------ | ----- | ---------- |
| `preloop.api_usage.dropped` | Stable | none |
| `preloop.api_usage.failed` | Stable | none |
| `gen_ai.client.operation.duration` | Experimental | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model` |
| `db.client.connections.usage` | Experimental | `preloop.db.engine` |
| `db.client.connections.overflow` | Experimental | `preloop.db.engine` |
| `db.client.connections.max` | Experimental | `preloop.db.engine` |
