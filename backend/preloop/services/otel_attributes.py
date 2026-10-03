"""Machine-readable OTLP attribute and metric stability registry.

The policy lives in ``docs/guide/otlp-attribute-stability.md``. Every
attribute or metric the exporter emits must appear in exactly one of the
stable or experimental maps below; ``tests/services/test_otel_attributes.py``
enforces this so the policy cannot drift silently.

Stable: name and meaning are kept within a major version. Renames and
removals follow the deprecation window in the policy (CHANGELOG
"Telemetry" entry, then both names emitted during the overlap).

Experimental: follows the upstream OpenTelemetry GenAI semantic
conventions, which are still in Development upstream. May change when
upstream changes, with the same overlap where practical.
"""

from __future__ import annotations

STABLE_SPAN_ATTRIBUTES: dict[str, str] = {
    "preloop.account.id": "Preloop account id that owns the request.",
    "preloop.api_usage.id": "Id of the ApiUsage ledger row behind the span.",
    "preloop.usage.estimated_cost_usd": (
        "Recorded cost in USD, equal to the ApiUsage row."
    ),
    "preloop.usage.cost_source": "Where the recorded cost came from.",
    "preloop.mcp.server_name": "MCP server that served a tool call.",
    "http.response.status_code": (
        "HTTP status of the governed call (OpenTelemetry stable HTTP semconv)."
    ),
}

EXPERIMENTAL_SPAN_ATTRIBUTES: dict[str, str] = {
    "gen_ai.operation.name": "GenAI operation (chat, embeddings, execute_tool).",
    "gen_ai.provider.name": "Model provider name.",
    "gen_ai.request.model": "Requested model alias.",
    "gen_ai.conversation.id": "Preloop runtime session id.",
    "gen_ai.usage.input_tokens": "Input token count.",
    "gen_ai.usage.output_tokens": "Output token count.",
    "gen_ai.response.finish_reasons": "Finish reasons reported by the model.",
    "gen_ai.tool.name": "Name of the executed tool.",
    "gen_ai.tool.type": "Tool type (function).",
}

STABLE_METRICS: dict[str, str] = {
    "preloop.api_usage.dropped": "ApiUsage rows dropped before the database.",
    "preloop.api_usage.failed": "ApiUsage rows that failed to persist.",
}

EXPERIMENTAL_METRICS: dict[str, str] = {
    "gen_ai.client.operation.duration": (
        "Duration of a governed model or tool call, in seconds."
    ),
    "db.client.connections.usage": "Checked-out database connections per engine.",
    "db.client.connections.overflow": "Overflow database connections in use.",
    "db.client.connections.max": "Database connection ceiling per engine.",
}

STABLE_METRIC_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "preloop.api_usage.dropped": (),
    "preloop.api_usage.failed": (),
}

EXPERIMENTAL_METRIC_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "gen_ai.client.operation.duration": (
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
    ),
    "db.client.connections.usage": ("preloop.db.engine",),
    "db.client.connections.overflow": ("preloop.db.engine",),
    "db.client.connections.max": ("preloop.db.engine",),
}

ALL_SPAN_ATTRIBUTES = frozenset(STABLE_SPAN_ATTRIBUTES) | frozenset(
    EXPERIMENTAL_SPAN_ATTRIBUTES
)
